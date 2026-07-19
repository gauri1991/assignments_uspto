"""Tests for the CPC data-fetch layer and portfolio matcher (Phase 1 core modules).

No test touches the real network: the API source is exercised through an injected fake transport.
"""

from __future__ import annotations

import json
import re
import urllib.error
from dataclasses import replace
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from uspto_assignments import (
    AttachCpcFileStep,
    BatchTemplate,
    CpcMatchStep,
    CpcRunContext,
    ExportStep,
    FetchCpcStep,
    LoadConfig,
    columns_after,
    export,
    run_preview,
    validate_template,
)
from uspto_assignments.batch import _step_from_dict
from uspto_assignments.cpcconfig import CpcConfig, load_config, save_config
from uspto_assignments.cpcmatch import (
    CLASS_MATCH_COLUMNS,
    HitRateError,
    _normalize_grant,
    attach_cpc,
    attach_cpc_from_file,
    grain_of,
    load_portfolio_footprint,
    match_portfolio,
    reduce_codes,
)
from uspto_assignments.datasource import (
    ApiAuthError,
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


_QUERY_IDS = re.compile(r"patentNumber:\(([^)]*)\)")


def _fake_transport(catalog: dict[str, list[str]]) -> Any:
    """Return a transport that answers an ODP query's OR-joined patent numbers from ``catalog``."""
    calls: list[int] = []

    def transport(_url: str, body: bytes, headers: dict[str, str]) -> bytes:
        assert headers.get("X-API-KEY")  # key must be sent
        query = json.loads(body.decode("utf-8"))["q"]
        match = _QUERY_IDS.search(query)
        ids = match.group(1).split(" OR ") if match and match.group(1) else []
        calls.append(len(ids))
        records = [
            {"applicationMetaData": {"patentNumber": pid, "cpcClassificationBag": catalog[pid]}}
            for pid in ids
            if pid in catalog
        ]
        return json.dumps({"patentFileWrapperDataBag": records}).encode("utf-8")

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


def test_api_source_preserves_padded_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USPTO_ODP_API_KEY", "secret")

    def padded_transport(_url: str, body: bytes, headers: dict[str, str]) -> bytes:
        assert headers.get("X-API-KEY")
        records = [
            {
                "applicationMetaData": {
                    "patentNumber": "10000001",
                    "cpcClassificationBag": ["G01S   7/4863", "G01S  17/894"],
                }
            }
        ]
        return json.dumps({"patentFileWrapperDataBag": records}).encode("utf-8")

    source = UsptoOdpApiSource(config=CpcConfig().source, transport=padded_transport)
    # fetch preserves the raw (space-padded) symbols; grain_of strips them downstream.
    assert source.fetch(["10000001"]) == {"10000001": ["G01S   7/4863", "G01S  17/894"]}


def test_api_source_raises_on_http_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USPTO_ODP_API_KEY", "badkey")

    def failing_transport(url: str, _body: bytes, _headers: dict[str, str]) -> bytes:
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)  # type: ignore[arg-type]

    config = CpcConfig()
    config.source.retries = 2
    source = UsptoOdpApiSource(config=config.source, transport=failing_transport)
    with pytest.raises(ApiAuthError, match="HTTP 403"):  # 4xx is not retried
        source.fetch(["10000001"])


def test_api_source_treats_404_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USPTO_ODP_API_KEY", "secret")

    def not_found_transport(url: str, _body: bytes, _headers: dict[str, str]) -> bytes:
        # ODP returns 404 for a query that matches no records — an empty result, not an error.
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    source = UsptoOdpApiSource(config=CpcConfig().source, transport=not_found_transport)
    assert source.fetch(["6534515", "99999999999"]) == {}  # no raise; all unresolved


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


def test_attach_cpc_normalizes_publication_style_file_keys(tmp_path: Path) -> None:
    """Regression: PatSeer-style file keys (US prefix, kind suffix, commas) must join, not 0-hit."""
    tsv = tmp_path / "cpc.tsv"
    _write_cpc_tsv(tsv, {"US10000001B2": ["H04L9/32"], "USD0912345S1": ["D14/138"]})
    config = CpcConfig()
    config.source.type = "local_file"
    config.source.path = str(tsv)
    config.cache.path = str(tmp_path / "cache")
    cache = CpcCache(config, make_source(config), now=1000.0)
    table = _flat(["10000001", "D912345"], ["B2", "S1"])
    out, stats = attach_cpc(
        table, number_column="doc_number", kind_column="doc_kind", cache=cache, allow_network=False
    )
    assert stats.eligible == 2 and stats.found == 2 and stats.hit_rate == 1.0
    assert [r["cpc_lookup_status"] for r in out.to_pylist()] == ["found", "found"]


def test_normalize_grant_accepts_publication_shapes() -> None:
    """Portfolio/footprint files may hold publication-style ids; all normalize to the grant key."""
    assert _normalize_grant("US10000001B2") == "10000001"
    assert _normalize_grant("USD0912345S1") == "D912345"
    assert _normalize_grant("8,000,001") == "8000001"
    assert _normalize_grant("RE034567E") == "RE34567"
    assert _normalize_grant("10000001") == "10000001"  # bare ids unchanged


def test_attach_cpc_hit_rate_low_on_genuine_mismatch(tmp_path: Path) -> None:
    # A file keyed by unrelated ids still yields 0 hits — the hit-rate guard remains meaningful.
    tsv = tmp_path / "cpc.tsv"
    _write_cpc_tsv(tsv, {"99999999": ["H04L9/32"]})
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
    per, overall, class_table, report = match_portfolio(
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
    assert class_table.num_rows == 0  # not requested → empty class table


def test_match_portfolio_emits_class_matches(tmp_path: Path) -> None:
    # ACME holds two grants; the portfolio patent's footprint is {G06F, H04L}. The class table must
    # record one row per (portfolio patent × buyer patent × shared class), grain-reduced.
    tsv = tmp_path / "cpc.tsv"
    _write_cpc_tsv(
        tsv,
        {
            "10000001": ["G06F16/00", "H04L9/32"],  # ACME — shares BOTH classes
            "10000002": ["H04L1/00"],  # ACME — shares only H04L
            "10000003": ["A61K31/00"],  # BETA — shares nothing with this portfolio patent
        },
    )
    table = _flat(
        ["10000001", "10000002", "10000003"],
        ["B2", "B2", "B2"],
        assignee_names_canonical=["ACME CORP", "ACME CORP", "BETA PHARMA"],
        transaction_date=["20190101", "20200101", "20230101"],
    )
    enriched = _attach(table, tsv, tmp_path)
    footprints = {"9000001": {"G06F", "H04L"}}
    _per, _overall, class_table, _report = match_portfolio(
        enriched,
        footprints,
        config=CpcConfig().match,
        buyer_column="assignee_names_canonical",
        number_column="doc_number",
        kind_column="doc_kind",
        date_column="transaction_date",
        emit_class_matches=True,
    )
    assert class_table.column_names == CLASS_MATCH_COLUMNS
    got = {
        (r["portfolio_patent"], r["buyer"], r["buyer_patent"], r["cpc_class"], r["year"])
        for r in class_table.to_pylist()
    }
    assert got == {
        ("9000001", "ACME CORP", "10000001", "G06F", "2019"),
        ("9000001", "ACME CORP", "10000001", "H04L", "2019"),
        ("9000001", "ACME CORP", "10000002", "H04L", "2020"),
    }  # BETA never appears — it shares no class


def test_class_matches_honor_min_in_domain_filter(tmp_path: Path) -> None:
    # With min_in_domain_patents=2, a buyer with only ONE matching patent is dropped from the ranked
    # table — and must therefore also produce NO class rows (the two outputs never disagree).
    tsv = tmp_path / "cpc.tsv"
    _write_cpc_tsv(
        tsv, {"10000001": ["H04L9/32"], "10000002": ["H04L1/00"], "10000003": ["H04L9/00"]}
    )
    table = _flat(
        ["10000001", "10000002", "10000003"],
        ["B2", "B2", "B2"],
        assignee_names_canonical=["ACME CORP", "ACME CORP", "SOLO INC"],  # SOLO has just one
        transaction_date=["20190101", "20200101", "20210101"],
    )
    enriched = _attach(table, tsv, tmp_path)
    config = replace(CpcConfig().match, min_in_domain_patents=2)
    per, _overall, class_table, _report = match_portfolio(
        enriched,
        {"9000001": {"H04L"}},
        config=config,
        buyer_column="assignee_names_canonical",
        number_column="doc_number",
        kind_column="doc_kind",
        date_column="transaction_date",
        emit_class_matches=True,
    )
    assert {r["buyer"] for r in per.to_pylist()} == {"ACME CORP"}  # SOLO dropped from ranking
    assert {r["buyer"] for r in class_table.to_pylist()} == {"ACME CORP"}  # and from class rows


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


# ------------------------------------------------------------------ batch steps


def test_cpc_steps_roundtrip() -> None:
    for step in (
        FetchCpcStep(),
        CpcMatchStep(portfolio_path="p.txt"),
        CpcMatchStep(
            portfolio_path="fp.csv", portfolio_mode="footprint_file", emit_class_matches=True
        ),
    ):
        assert _step_from_dict(step.to_dict()).to_dict() == step.to_dict()


def test_cpc_steps_in_columns_after() -> None:
    steps = [FetchCpcStep(table="flat"), CpcMatchStep(table="flat", emit_class_matches=True)]
    cols = columns_after(LoadConfig(), steps, upto=2)
    assert {"cpc_codes", "cpc_subclasses", "cpc_lookup_status"} <= set(cols["flat"])
    assert "portfolio_patent" in cols["matched_buyers_by_portfolio_patent"]
    # the class-match output table is advertised only when emit_class_matches is on
    assert "cpc_class" in cols["matched_cpc_classes"]
    assert "matched_cpc_classes" not in columns_after(
        LoadConfig(), [CpcMatchStep(table="flat")], upto=1
    )


def test_cpc_match_step_validates_portfolio_file(tmp_path: Path) -> None:
    step = CpcMatchStep(
        portfolio_mode="footprint_file", portfolio_path=str(tmp_path / "missing.csv")
    )
    warnings = validate_template(LoadConfig(), [FetchCpcStep(), step])
    assert any("portfolio file not found" in w for w in warnings)


FIXTURE = Path(__file__).parent / "fixtures" / "buyer_sample.xml"


def _local_ctx(tmp_path: Path, tsv: Path) -> CpcRunContext:
    config = CpcConfig()
    config.source.type = "local_file"
    config.source.path = str(tsv)
    config.cache.path = str(tmp_path / "cache")
    return CpcRunContext(config=config, allow_network=False)


def test_end_to_end_fetch_then_match(tmp_path: Path) -> None:
    """Two-step flow on a real fixture: fetch_cpc enriches, cpc_match ranks buyers per patent."""
    tsv = tmp_path / "cpc.tsv"
    _write_cpc_tsv(
        tsv,
        {
            "10987654": ["H04L9/32"],  # H04L grant
            "11222333": ["H04L1/00"],  # H04L grant
            "9555444": ["A61K31/00"],  # A61K grant (off-domain)
        },
    )
    footprint = tmp_path / "portfolio.csv"
    footprint.write_text(
        "patent,cpc\n9000001,H04L9/32\n", encoding="utf-8"
    )  # sales package in H04L

    template = BatchTemplate(
        name="cpc",
        steps=[
            FetchCpcStep(table="flat", column="doc_number", kind_column="doc_kind"),
            CpcMatchStep(
                table="flat",
                portfolio_mode="footprint_file",
                portfolio_path=str(footprint),
                buyer_column="assignee_names",
                number_column="doc_number",
                kind_column="doc_kind",
                date_column="transaction_date",
            ),
            ExportStep(fmt="parquet", tables=["matched_buyers_by_portfolio_patent"]),
        ],
    )
    tables, _stats = run_preview(template, FIXTURE, limit=100, cpc_ctx=_local_ctx(tmp_path, tsv))

    flat = tables["flat"]
    assert {"cpc_codes", "cpc_subclasses", "cpc_lookup_status"} <= set(flat.column_names)
    ranked = tables["matched_buyers_by_portfolio_patent"].to_pylist()
    buyers = {row["buyer"] for row in ranked}
    assert all(row["portfolio_patent"] == "9000001" for row in ranked)
    assert "WIDGET CORP" in buyers  # bought H04L grant 10987654
    assert "ZORPTECH SYSTEMS INCORPORATED" not in buyers  # only A61K — off-domain, excluded
    assert all(row["rank"] >= 1 for row in ranked)


def test_offline_attach_then_match_with_class_matches(tmp_path: Path) -> None:
    """The offline 13→14 chain in one run: attach CPC from a file, match a footprint, no network."""
    patseer = tmp_path / "patseer.csv"  # PatSeer-style export: Publication Number, CPC (;-joined)
    patseer.write_text(
        "Publication Number,CPC\n"
        "10987654,G06F16/00;H04L9/32\n"
        "11222333,H04L9/32\n"
        "9555444,A61K31/00\n",
        encoding="utf-8",
    )
    footprint = tmp_path / "footprint.csv"  # positional patent,cpc — one code per row
    footprint.write_text(
        "patent,cpc\n9111111,G06F16/00\n9111111,H04L9/32\n9222222,A61K31/00\n", encoding="utf-8"
    )
    template = BatchTemplate(
        name="offline",
        steps=[
            AttachCpcFileStep(
                table="flat",
                column="doc_number",
                kind_column="doc_kind",
                source_path=str(patseer),
                patent_column="Publication Number",
                code_column="CPC",
                separator=";",
            ),
            CpcMatchStep(
                table="flat",
                portfolio_mode="footprint_file",
                portfolio_path=str(footprint),
                buyer_column="assignee_names",
                number_column="doc_number",
                kind_column="doc_kind",
                date_column="transaction_date",
                emit_class_matches=True,
            ),
            ExportStep(fmt="csv", tables=["matched_cpc_classes"]),
        ],
    )
    # local_file ctx (never fetched in footprint_file mode) guarantees no network path is taken.
    empty_tsv = tmp_path / "src.tsv"
    empty_tsv.write_text("patent_id\tcpc_group\n", encoding="utf-8")
    tables, _stats = run_preview(
        template, FIXTURE, limit=100, cpc_ctx=_local_ctx(tmp_path, empty_tsv)
    )

    classes = tables["matched_cpc_classes"]
    assert classes.column_names == CLASS_MATCH_COLUMNS
    triples = {
        (r["portfolio_patent"], r["buyer_patent"], r["cpc_class"]) for r in classes.to_pylist()
    }
    assert {
        ("9111111", "10987654", "G06F"),  # grant 10987654 shares both G06F and H04L
        ("9111111", "10987654", "H04L"),
        ("9111111", "11222333", "H04L"),  # grant 11222333 shares only H04L
        ("9222222", "9555444", "A61K"),  # the A61K portfolio patent matches the A61K grant
    } <= triples
    assert ("9111111", "9555444", "A61K") not in triples  # A61K grant ≠ G06F/H04L portfolio patent


def test_list_columns_export_to_csv(tmp_path: Path) -> None:
    """CPC list columns (cpc_codes/shared_codes) must flatten to strings for CSV/Excel."""
    table = pa.table(
        {
            "buyer": ["ACME"],
            "shared_codes": pa.array([["H04L", "G06F"]], type=pa.list_(pa.string())),
        }
    )
    out = tmp_path / "out.csv"
    export(table, out, "csv")
    text = out.read_text(encoding="utf-8")
    assert "H04L; G06F" in text  # joined, not an array


def test_fetch_cpc_offline_reports_uncached(tmp_path: Path) -> None:
    """With the API source offline, fetch_cpc attaches nothing and flags uncached grants."""
    events: list[str] = []
    config = CpcConfig()  # default uspto_odp_api + offline_only=True
    config.cache.path = str(tmp_path / "cache")
    ctx = CpcRunContext(config=config, allow_network=True)  # offline_only still wins
    template = BatchTemplate(
        name="fetch",
        steps=[FetchCpcStep(table="flat", column="doc_number", kind_column="doc_kind")],
    )
    run_preview(
        template,
        FIXTURE,
        limit=100,
        cpc_ctx=ctx,
        on_event=lambda e: events.append(e.message),
    )
    assert any("uncached" in m for m in events)


# ------------------------------------------------------------------ attach CPC from file


def test_attach_cpc_from_file_one_code_per_row(tmp_path: Path) -> None:
    tsv = tmp_path / "cpc.tsv"
    tsv.write_text(
        "patent_id\tcpc_group\n10000001\tH04L9/32\n10000001\tG06F3/01\n10000002\tH01L21/02\n",
        encoding="utf-8",
    )
    table = pa.table(
        {"doc_number": ["10000001", "10000002", "99999999"], "doc_kind": ["B2", "B2", "B2"]}
    )
    out, stats = attach_cpc_from_file(
        table,
        number_column="doc_number",
        kind_column="doc_kind",
        source_path=tsv,
        patent_column="patent_id",
        code_column="cpc_group",
        separator="",  # one code per row
    )
    codes = out.column("cpc_codes").to_pylist()
    status = out.column("cpc_lookup_status").to_pylist()
    assert sorted(codes[0] or []) == ["G06F3/01", "H04L9/32"]
    assert codes[1] == ["H01L21/02"]
    assert status == ["found", "found", "not_found"]
    assert stats.found == 2


def test_attach_cpc_from_file_patseer_multi_code_cell(tmp_path: Path) -> None:
    csv = tmp_path / "patseer.csv"
    csv.write_text(
        "Publication Number,CPC\n10000001,H04L9/32; G06F3/01\n10000002,H01L21/02\n",
        encoding="utf-8",
    )
    table = pa.table({"doc_number": ["10000001", "10000002"], "doc_kind": ["B2", "B2"]})
    out, _stats = attach_cpc_from_file(
        table,
        number_column="doc_number",
        kind_column="doc_kind",
        source_path=csv,
        patent_column="Publication Number",
        code_column="CPC",
        separator=";",  # one cell packs several CPCs
    )
    codes = out.column("cpc_codes").to_pylist()
    assert sorted(codes[0] or []) == ["G06F3/01", "H04L9/32"]  # the cell was split
    assert out.column("cpc_subclasses").to_pylist()[0] == ["H04L", "G06F"]


def test_attach_cpc_file_step_roundtrip_schema_and_apply(tmp_path: Path) -> None:
    step = AttachCpcFileStep(
        table="flat", source_path="/x/patseer.csv", patent_column="Pub", code_column="CPCs"
    )
    assert _step_from_dict(step.to_dict()).to_dict() == step.to_dict()
    cols = columns_after(LoadConfig(), [step], upto=1)["flat"]
    for name in ("cpc_codes", "cpc_subclasses", "cpc_lookup_status"):
        assert name in cols


def test_cache_save_merges_concurrent_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: two caches sharing the file must union their fetches, not last-writer-wins."""
    monkeypatch.setenv("USPTO_ODP_API_KEY", "secret")
    config = _api_config(tmp_path, offline_only=False)
    cache_a = CpcCache(
        config,
        make_source(config, transport=_fake_transport({"10000001": ["H04L9/32"]})),
        now=1000.0,
    )
    # b is constructed (loads the empty file) BEFORE a saves — like a parallel batch worker.
    cache_b = CpcCache(
        config,
        make_source(config, transport=_fake_transport({"10000002": ["G06F3/01"]})),
        now=1000.0,
    )
    cache_a.resolve(["10000001"], allow_network=True)
    cache_b.resolve(["10000002"], allow_network=True)  # must merge, not overwrite, a's entry

    offline = _api_config(tmp_path, offline_only=True)
    cache_c = CpcCache(offline, make_source(offline, transport=_fake_transport({})), now=1000.0)
    result = cache_c.resolve(["10000001", "10000002"], allow_network=False)
    assert result.uncached_offline == []
    assert result.found() == {"10000001": ["H04L9/32"], "10000002": ["G06F3/01"]}
    assert not list(Path(config.cache.path).glob("*.tmp"))  # atomic rename leaves no temp files
