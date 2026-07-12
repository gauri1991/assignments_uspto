"""Tests for the CPC data-fetch layer and portfolio matcher (Phase 1 core modules).

No test touches the real network: the API source is exercised through an injected fake transport.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from uspto_assignments.cpcconfig import CpcConfig, load_config, save_config
from uspto_assignments.cpcmatch import (
    HitRateError,
    attach_cpc,
    grain_of,
    load_portfolio_footprint,
    match_portfolio,
    reduce_codes,
)
from uspto_assignments.datasource import (
    ApiBudgetError,
    CpcCache,
    LocalFileCpcSource,
    OfflineError,
    UsptoOdpApiSource,
    make_source,
)

# ------------------------------------------------------------------ config


def test_config_roundtrip_and_defaults() -> None:
    config = CpcConfig()
    assert config.source.type == "uspto_odp_api"
    assert config.source.offline_only is True  # offline by default
    assert config.source.api_key_env  # a name, never a key
    assert config.match.grain == "subclass"
    restored = CpcConfig.from_dict(config.to_dict())
    assert restored.to_dict() == config.to_dict()


def test_config_rejects_unknown_enums() -> None:
    with pytest.raises(ValueError, match="unknown CPC source type"):
        CpcConfig.from_dict({"source": {"type": "carrier_pigeon"}})
    with pytest.raises(ValueError, match="unknown CPC grain"):
        CpcConfig.from_dict({"match": {"grain": "molecule"}})


def test_config_save_load_never_stores_key(tmp_path: Path) -> None:
    path = tmp_path / "cpc_config.json"
    config = CpcConfig()
    config.source.api_key_env = "MY_KEY_VAR"
    save_config(config, path)
    text = path.read_text(encoding="utf-8")
    assert "MY_KEY_VAR" in text and "api_key" in text
    assert load_config(path).source.api_key_env == "MY_KEY_VAR"


def test_load_config_missing_returns_defaults(tmp_path: Path) -> None:
    assert load_config(tmp_path / "absent.json").source.type == "uspto_odp_api"


# ------------------------------------------------------------------ grain


def test_grain_reduction() -> None:
    assert grain_of("H04L9/32", "subclass") == "H04L"
    assert grain_of("H04L9/32", "main_group") == "H04L9"
    assert grain_of("h04l 9/32", "full_symbol") == "H04L9/32"
    assert reduce_codes(["H04L9/32", "H04L1/00", "G06F3/01"], "subclass") == ["H04L", "G06F"]


# ------------------------------------------------------------------ fake transport


def _fake_transport(catalog: dict[str, list[str]]) -> Any:
    """Return a transport that answers a POST body's ``q.patent_id`` list from ``catalog``."""
    calls: list[int] = []

    def transport(_url: str, body: bytes, headers: dict[str, str]) -> bytes:
        assert headers.get("X-Api-Key")  # key must be sent
        ids = json.loads(body.decode("utf-8"))["q"]["patent_id"]
        calls.append(len(ids))
        patents = [
            {"patent_id": pid, "cpc_current": [{"cpc_group_id": c} for c in catalog[pid]]}
            for pid in ids
            if pid in catalog
        ]
        return json.dumps({"patents": patents}).encode("utf-8")

    transport.calls = calls  # type: ignore[attr-defined]  # test introspection handle
    return transport


# ------------------------------------------------------------------ API source


def test_api_source_parses_and_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USPTO_ODP_API_KEY", "secret")
    config = CpcConfig()
    config.source.batch_size = 2
    catalog = {"10000001": ["H04L9/32"], "10000002": ["G06F3/01"], "10000003": ["H01L21/02"]}
    transport = _fake_transport(catalog)
    source = UsptoOdpApiSource(config=config.source, transport=transport)
    result = source.fetch(["10000001", "10000002", "10000003"])
    assert result == catalog
    assert transport.calls == [2, 1]  # batched at size 2


def test_api_source_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USPTO_ODP_API_KEY", raising=False)
    source = UsptoOdpApiSource(config=CpcConfig().source, transport=_fake_transport({}))
    with pytest.raises(OfflineError, match="USPTO_ODP_API_KEY"):
        source.fetch(["10000001"])


def test_api_source_enforces_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USPTO_ODP_API_KEY", "secret")
    config = CpcConfig()
    config.source.batch_size = 1
    config.source.max_api_calls = 2
    source = UsptoOdpApiSource(config=config.source, transport=_fake_transport({}))
    with pytest.raises(ApiBudgetError, match="max_api_calls"):
        source.fetch(["1", "2", "3"])


# ------------------------------------------------------------------ local file source


def _write_cpc_tsv(path: Path, rows: dict[str, list[str]]) -> None:
    lines = ["patent_id\tcpc_group"]
    for pid, codes in rows.items():
        lines.extend(f"{pid}\t{code}" for code in codes)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_local_file_source(tmp_path: Path) -> None:
    tsv = tmp_path / "cpc.tsv"
    _write_cpc_tsv(tsv, {"10000001": ["H04L9/32", "G06F3/01"], "10000002": ["H01L21/02"]})
    source = LocalFileCpcSource(path=tsv)
    result = source.fetch(["10000001", "10000002", "99999999"])
    assert sorted(result["10000001"]) == ["G06F3/01", "H04L9/32"]
    assert result["10000002"] == ["H01L21/02"]
    assert "99999999" not in result  # unknown id absent


# ------------------------------------------------------------------ cache + offline posture


def _api_config(tmp_path: Path, *, offline_only: bool) -> CpcConfig:
    config = CpcConfig()
    config.source.offline_only = offline_only
    config.cache.path = str(tmp_path / "cache")
    return config


def test_cache_offline_blocks_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USPTO_ODP_API_KEY", "secret")
    config = _api_config(tmp_path, offline_only=True)
    transport = _fake_transport({"10000001": ["H04L9/32"]})
    cache = CpcCache(config, make_source(config, transport=transport), now=1000.0)
    result = cache.resolve(["10000001"], allow_network=True)  # offline_only wins over allow_network
    assert result.uncached_offline == ["10000001"]
    assert result.fetched == 0
    assert transport.calls == []  # never called


def test_cache_fetches_then_serves_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USPTO_ODP_API_KEY", "secret")
    online = _api_config(tmp_path, offline_only=False)
    transport = _fake_transport({"10000001": ["H04L9/32"], "10000002": ["G06F3/01"]})
    cache = CpcCache(online, make_source(online, transport=transport), now=1000.0)
    first = cache.resolve(["10000001", "10000002"], allow_network=True)
    assert first.fetched == 2 and first.found() == {
        "10000001": ["H04L9/32"],
        "10000002": ["G06F3/01"],
    }
    assert (Path(online.cache.path) / "cpc_cache.parquet").is_file()

    # A fresh offline cache over the same file serves the hits without any network.
    offline = _api_config(tmp_path, offline_only=True)
    fresh_transport = _fake_transport({})  # would raise if asked (no catalog)
    cache2 = CpcCache(offline, make_source(offline, transport=fresh_transport), now=1000.0)
    second = cache2.resolve(["10000001", "10000002"], allow_network=False)
    assert second.uncached_offline == []
    assert fresh_transport.calls == []


def test_cache_ttl_expiry_refetches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USPTO_ODP_API_KEY", "secret")
    config = _api_config(tmp_path, offline_only=False)
    config.cache.ttl_days = 1
    transport = _fake_transport({"10000001": ["H04L9/32"]})
    CpcCache(config, make_source(config, transport=transport), now=1000.0).resolve(
        ["10000001"], allow_network=True
    )
    assert transport.calls == [1]
    # Two days later, the entry is stale → a new resolve refetches.
    later = CpcCache(config, make_source(config, transport=transport), now=1000.0 + 2 * 86400)
    later.resolve(["10000001"], allow_network=True)
    assert transport.calls == [1, 1]


def test_local_file_source_works_offline(tmp_path: Path) -> None:
    tsv = tmp_path / "cpc.tsv"
    _write_cpc_tsv(tsv, {"10000001": ["H04L9/32"]})
    config = CpcConfig()
    config.source.type = "local_file"
    config.source.path = str(tsv)
    config.source.offline_only = True  # irrelevant: a file read is not network
    config.cache.path = str(tmp_path / "cache")
    cache = CpcCache(config, make_source(config), now=1000.0)
    result = cache.resolve(["10000001"], allow_network=False)
    assert result.found() == {"10000001": ["H04L9/32"]}


# ------------------------------------------------------------------ attach_cpc + hit-rate guard


def _flat(numbers: list[str], kinds: list[str], **extra: list[Any]) -> pa.Table:
    data: dict[str, list[Any]] = {"doc_number": numbers, "doc_kind": kinds}
    data.update(extra)
    return pa.table(data)


def test_attach_cpc_routes_grants_only(tmp_path: Path) -> None:
    tsv = tmp_path / "cpc.tsv"
    _write_cpc_tsv(tsv, {"10000001": ["H04L9/32", "H04L1/00"]})
    config = CpcConfig()
    config.source.type = "local_file"
    config.source.path = str(tsv)
    config.cache.path = str(tmp_path / "cache")
    cache = CpcCache(config, make_source(config), now=1000.0)
    table = _flat(["10000001", "16123456"], ["B2", "X0"])  # a grant + an application
    out, stats = attach_cpc(
        table, number_column="doc_number", kind_column="doc_kind", cache=cache, allow_network=False
    )
    rows = out.to_pylist()
    grant = next(r for r in rows if r["doc_number"] == "10000001")
    app = next(r for r in rows if r["doc_number"] == "16123456")
    assert grant["cpc_lookup_status"] == "found"
    assert sorted(grant["cpc_subclasses"]) == ["H04L"]
    assert app["cpc_lookup_status"] == "na" and app["cpc_codes"] == []
    assert stats.eligible == 1 and stats.found == 1 and stats.hit_rate == 1.0


def test_attach_cpc_hit_rate_low_on_format_mismatch(tmp_path: Path) -> None:
    # CPC file keyed with kind-suffixed ids; our normalization produces bare grant numbers → 0 hits.
    tsv = tmp_path / "cpc.tsv"
    _write_cpc_tsv(tsv, {"US10000001B2": ["H04L9/32"]})
    config = CpcConfig()
    config.source.type = "local_file"
    config.source.path = str(tsv)
    config.cache.path = str(tmp_path / "cache")
    cache = CpcCache(config, make_source(config), now=1000.0)
    table = _flat(["10000001"], ["B2"])
    _out, stats = attach_cpc(
        table, number_column="doc_number", kind_column="doc_kind", cache=cache, allow_network=False
    )
    assert stats.eligible == 1 and stats.found == 0 and stats.hit_rate == 0.0


# ------------------------------------------------------------------ portfolio footprint + match


def test_footprint_from_file(tmp_path: Path) -> None:
    footprint = tmp_path / "portfolio.csv"
    footprint.write_text("patent,cpc\n9000001,H04L9/32\n9000001,G06F3/01\n", encoding="utf-8")
    footprints, stats = load_portfolio_footprint(
        mode="footprint_file", path=footprint, grain="subclass", cache=None, allow_network=False
    )
    assert footprints == {"9000001": {"H04L", "G06F"}}
    assert stats.eligible == 0  # file mode does no lookup


def test_footprint_from_patent_list(tmp_path: Path) -> None:
    tsv = tmp_path / "cpc.tsv"
    _write_cpc_tsv(tsv, {"9000001": ["H04L9/32"]})
    config = CpcConfig()
    config.source.type = "local_file"
    config.source.path = str(tsv)
    config.cache.path = str(tmp_path / "cache")
    cache = CpcCache(config, make_source(config), now=1000.0)
    portfolio = tmp_path / "portfolio.txt"
    portfolio.write_text("9000001\nUS09000001\n", encoding="utf-8")  # same id, two spellings
    footprints, stats = load_portfolio_footprint(
        mode="patent_list", path=portfolio, grain="subclass", cache=cache, allow_network=False
    )
    assert footprints == {"9000001": {"H04L"}}
    assert stats.eligible == 1 and stats.found == 1


def _attach(table: pa.Table, tsv: Path, tmp_path: Path) -> pa.Table:
    config = CpcConfig()
    config.source.type = "local_file"
    config.source.path = str(tsv)
    config.cache.path = str(tmp_path / "cache")
    cache = CpcCache(config, make_source(config), now=1000.0)
    out, _stats = attach_cpc(
        table, number_column="doc_number", kind_column="doc_kind", cache=cache, allow_network=False
    )
    return out


def test_match_portfolio_ranks_buyers(tmp_path: Path) -> None:
    tsv = tmp_path / "cpc.tsv"
    _write_cpc_tsv(
        tsv,
        {
            "10000001": ["H04L9/32"],  # Acme patent — H04L
            "10000002": ["H04L1/00"],  # Acme patent — H04L (in-domain volume)
            "10000003": ["A61K31/00"],  # Beta patent — A61K (off-domain)
        },
    )
    table = _flat(
        ["10000001", "10000002", "10000003"],
        ["B2", "B2", "B2"],
        assignee_names_canonical=["ACME CORP", "ACME CORP", "BETA PHARMA"],
        transaction_date=["20240101", "20250101", "20230101"],
    )
    enriched = _attach(table, tsv, tmp_path)
    footprints = {"9000001": {"H04L"}}  # the sales-package patent is in H04L
    config = CpcConfig().match
    per, overall, report = match_portfolio(
        enriched,
        footprints,
        config=config,
        buyer_column="assignee_names_canonical",
        number_column="doc_number",
        kind_column="doc_kind",
        date_column="transaction_date",
    )
    per_rows = per.to_pylist()
    assert [r["buyer"] for r in per_rows] == ["ACME CORP"]  # only the H04L buyer matches
    acme = per_rows[0]
    assert acme["in_domain_patents"] == 2 and acme["rank"] == 1
    assert acme["last_acquisition_date"] == "2025"
    assert report.buyer_stats.found == 3
    assert overall.num_rows == 1


def test_match_portfolio_requires_attached_cpc() -> None:
    table = pa.table({"doc_number": ["10000001"], "doc_kind": ["B2"], "buyer": ["X"]})
    with pytest.raises(ValueError, match="fetch_cpc"):
        match_portfolio(
            table,
            {"9000001": {"H04L"}},
            config=CpcConfig().match,
            buyer_column="buyer",
            number_column="doc_number",
            kind_column="doc_kind",
            date_column="",
        )


def test_match_portfolio_aborts_on_low_hit_rate(tmp_path: Path) -> None:
    # A grant row whose CPC never resolved (empty cpc_codes) → 0% hit-rate → HitRateError.
    table = pa.table(
        {
            "doc_number": ["10000001"],
            "doc_kind": ["B2"],
            "buyer": ["X"],
            "cpc_codes": pa.array([[]], type=pa.list_(pa.string())),
        }
    )
    with pytest.raises(HitRateError, match="hit-rate"):
        match_portfolio(
            table,
            {"9000001": {"H04L"}},
            config=CpcConfig().match,
            buyer_column="buyer",
            number_column="doc_number",
            kind_column="doc_kind",
            date_column="",
        )
