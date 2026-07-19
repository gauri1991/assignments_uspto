"""Per-patent CPC portfolio matching: attach CPC to patents, then rank same-domain buyers.

Two operations, kept separate so they compose as two batch steps:

- :func:`attach_cpc` — enrich a table (one row per buyer×patent) with ``cpc_codes`` (full symbols)
  and ``cpc_subclasses`` (4-char grain) and a ``cpc_lookup_status``, routing to grants first and
  resolving CPC through the cache-first :class:`~uspto_assignments.datasource.CpcCache`.
- :func:`match_portfolio` — for each sales-package (portfolio) patent, compute the CPC overlap of
  every buyer patent against that patent's footprint, keep overlaps above a threshold, roll matches
  up to buyers, and rank buyers by overlap strength × recency × in-domain volume.

Number-format discipline is the correctness spine: patent numbers are normalized to the bare grant
number (:func:`~uspto_assignments.propid.normalize_patent_id`) on both sides before any join, and a
``cpc_hit_rate`` below the configured floor **aborts** (a format mismatch, not "no data") — distinct
from an all-offline-miss state, which reports "enable network to fetch".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb as _duckdb
import pyarrow as pa

from .cpcconfig import CpcGrain, CpcMatchConfig, OverlapMetric
from .datasource import CpcCache, LocalFileCpcSource
from .propid import doc_type_for, normalize_patent_id

duckdb: Any = _duckdb  # duckdb is under-typed; route through Any (matches ledger.py).

_CPC_CODES_COLUMN = "cpc_codes"
_CPC_SUBCLASS_COLUMN = "cpc_subclasses"
_CPC_STATUS_COLUMN = "cpc_lookup_status"
_SUBCLASS_LEN = 4
_RECENCY_BASE_YEAR = 2000  # recency component = last-acquisition year minus this baseline


class HitRateError(RuntimeError):
    """Raised when the CPC join hit-rate falls below the floor (a likely patent-number mismatch)."""


def grain_of(symbol: str, grain: CpcGrain) -> str:
    """Reduce a full CPC symbol (e.g. ``H04L9/32``) to the requested grain key.

    ``subclass`` → first 4 chars (``H04L``); ``main_group`` → the part before ``/`` (``H04L9``);
    ``full_symbol`` → the symbol itself. Whitespace is stripped first.
    """
    cleaned = symbol.replace(" ", "").strip().upper()
    if grain == "subclass":
        return cleaned[:_SUBCLASS_LEN]
    if grain == "main_group":
        return cleaned.split("/", 1)[0]
    return cleaned


def reduce_codes(codes: list[str], grain: CpcGrain) -> list[str]:
    """Distinct grain keys for a list of full CPC symbols (order preserved)."""
    return list(dict.fromkeys(grain_of(c, grain) for c in codes if c))


# Publication-number shapes seen in portfolio/CPC files (PatSeer "US7000123B2", "USD912345S1",
# "7,000,123 B2"): the number with a trailing kind code, which ``normalize_patent_id`` would
# otherwise pass through unmodified (its regex requires the string to end in digits).
_TRAILING_KIND_RE = re.compile(r"^((?:US)?[A-Z]{0,2}\d+)[A-Z]\d?$")


def _normalize_grant(raw: str) -> str:
    """Normalize a raw grant number to the bare-grant CPC key (forcing grant routing).

    Accepts publication-number shapes from user files: commas/spaces are stripped and a trailing
    kind code (``B2``, ``S1``, …) is removed before the standard grant normalization. Mirrors the
    SQL-side normalization in :class:`~uspto_assignments.datasource.LocalFileCpcSource`.
    """
    cleaned = raw.strip().upper().replace(",", "").replace(" ", "")
    match = _TRAILING_KIND_RE.match(cleaned)
    if match is not None:
        cleaned = match.group(1)
    return normalize_patent_id(cleaned, "grant")


@dataclass(slots=True)
class CpcStats:
    """Coverage of a CPC join: how many grant rows resolved, and the resulting hit-rate."""

    eligible: int  # grant rows (the join is only defined for grants)
    looked_up: int  # eligible rows we have a cache/fetch answer for (found or empty)
    found: int  # looked-up rows that resolved to ≥1 CPC symbol
    uncached_offline: int  # eligible rows with no cache entry and network disabled

    @property
    def hit_rate(self) -> float:
        return round(self.found / self.looked_up, 4) if self.looked_up else 0.0


def assert_hit_rate(stats: CpcStats, floor: float, *, side: str) -> None:
    """Abort if the join looks broken: format mismatch (looked-up but unresolved) or all-offline."""
    if stats.eligible and stats.uncached_offline == stats.eligible:
        raise HitRateError(
            f"{side}: all {stats.eligible} grant patents are uncached and the network is disabled "
            f"— enable network (or point at a local CPC file) to fetch their CPC codes first"
        )
    if stats.looked_up and stats.hit_rate < floor:
        raise HitRateError(
            f"{side}: CPC hit-rate {stats.hit_rate:.0%} is below the floor {floor:.0%} — the "
            f"patent numbers and the CPC source's key format are likely misaligned (expected bare "
            f"grant numbers like 10987654); check the source's patent id column/format"
        )


def _grant_ids(table: pa.Table, number_column: str, kind_column: str) -> list[str]:
    """Per-row normalized grant id (``""`` for non-grant rows, which are never looked up)."""
    numbers = table.column(number_column).to_pylist()
    kinds = (
        table.column(kind_column).to_pylist()
        if kind_column and kind_column in table.column_names
        else [None] * len(numbers)
    )
    return [
        normalize_patent_id(number, doc_type_for(number, kind))
        for number, kind in zip(numbers, kinds, strict=True)
    ]


def _attach_cpc_columns(
    table: pa.Table,
    grant_id: list[str],
    codes_by_id: dict[str, list[str]],
    offline_set: set[str],
) -> tuple[pa.Table, CpcStats]:
    """Add the ``cpc_codes``/``cpc_subclasses``/``cpc_lookup_status`` trio from a resolved lookup.

    Shared by the cache-backed :func:`attach_cpc` and the file-backed :func:`attach_cpc_from_file`
    so both produce byte-identical columns.
    """
    codes_col: list[list[str]] = []
    subclass_col: list[list[str]] = []
    status_col: list[str] = []
    found = 0
    for pid in grant_id:
        if not pid:
            codes_col.append([])
            subclass_col.append([])
            status_col.append("na")
            continue
        if pid in codes_by_id:
            codes = codes_by_id[pid]
            codes_col.append(codes)
            subclass_col.append(reduce_codes(codes, "subclass"))
            if codes:
                status_col.append("found")
                found += 1
            else:
                status_col.append("not_found")
        elif pid in offline_set:
            codes_col.append([])
            subclass_col.append([])
            status_col.append("uncached")
        else:
            codes_col.append([])
            subclass_col.append([])
            status_col.append("not_found")

    eligible = sum(1 for pid in grant_id if pid)
    looked_up = sum(1 for status in status_col if status in ("found", "not_found"))
    stats = CpcStats(
        eligible=eligible, looked_up=looked_up, found=found, uncached_offline=len(offline_set)
    )
    out = table
    for name, values, list_type in (
        (_CPC_CODES_COLUMN, codes_col, True),
        (_CPC_SUBCLASS_COLUMN, subclass_col, True),
        (_CPC_STATUS_COLUMN, status_col, False),
    ):
        if name in out.column_names:
            out = out.drop_columns([name])
        col_type = pa.list_(pa.string()) if list_type else pa.string()
        out = out.append_column(name, pa.array(values, type=col_type))
    return out, stats


def attach_cpc(
    table: pa.Table,
    *,
    number_column: str,
    kind_column: str,
    cache: CpcCache,
    allow_network: bool,
) -> tuple[pa.Table, CpcStats]:
    """Return ``table`` with ``cpc_codes``/``cpc_subclasses``/``cpc_lookup_status`` columns + stats.

    Non-grant rows are ``na`` (never looked up). Grant rows are normalized to the bare grant number,
    resolved through ``cache`` (which fetches misses only when the network is allowed), and get the
    distinct full CPC symbols plus their 4-char subclasses.
    """
    grant_id = _grant_ids(table, number_column, kind_column)
    wanted = [pid for pid in dict.fromkeys(grant_id) if pid]
    result = cache.resolve(wanted, allow_network=allow_network)
    return _attach_cpc_columns(table, grant_id, result.codes, set(result.uncached_offline))


def attach_cpc_from_file(  # noqa: PLR0913 - a clear entry point with keyword-only file options
    table: pa.Table,
    *,
    number_column: str,
    kind_column: str,
    source_path: Path,
    patent_column: str,
    code_column: str,
    separator: str = "",
) -> tuple[pa.Table, CpcStats]:
    """Attach CPC columns by joining a local file (PatSeer/CSV/Parquet) — no network, no cache.

    Same columns and semantics as :func:`attach_cpc`, but the CPC comes from ``source_path``
    (joined on ``patent_column``/``code_column``); ``separator`` splits multi-code cells.
    """
    grant_id = _grant_ids(table, number_column, kind_column)
    wanted = [pid for pid in dict.fromkeys(grant_id) if pid]
    source = LocalFileCpcSource(
        path=source_path,
        patent_column=patent_column,
        code_column=code_column,
        code_separator=separator,
    )
    codes_by_id = source.fetch(wanted)
    return _attach_cpc_columns(table, grant_id, codes_by_id, set())


def load_portfolio_footprint(
    *,
    mode: str,
    path: Path,
    grain: CpcGrain,
    cache: CpcCache | None,
    allow_network: bool,
) -> tuple[dict[str, set[str]], CpcStats]:
    """Build ``{portfolio_patent: {grain codes}}`` from a patent list or a footprint file.

    ``patent_list``: one grant number per line; each is normalized and its CPC footprint is resolved
    via ``cache``. ``footprint_file``: a CSV/TSV with a patent column and a CPC column (one code per
    row); no network is used. Returns the footprints plus the patent-list side's coverage stats.
    """
    if mode == "footprint_file":
        return _footprint_from_file(path, grain), CpcStats(0, 0, 0, 0)
    if cache is None:
        raise ValueError("patent_list mode requires a CPC cache/source")
    raw_numbers = [
        line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    grant_ids = [pid for pid in (_normalize_grant(n) for n in raw_numbers) if pid]
    unique = list(dict.fromkeys(grant_ids))
    result = cache.resolve(unique, allow_network=allow_network)
    footprints: dict[str, set[str]] = {}
    found = 0
    for pid in unique:
        codes = result.codes.get(pid, [])
        footprints[pid] = set(reduce_codes(codes, grain))
        if codes:
            found += 1
    looked_up = len(unique) - len(result.uncached_offline)
    stats = CpcStats(
        eligible=len(unique),
        looked_up=looked_up,
        found=found,
        uncached_offline=len(result.uncached_offline),
    )
    return footprints, stats


def _footprint_from_file(path: Path, grain: CpcGrain) -> dict[str, set[str]]:
    """Read a per-patent CPC footprint file (patent, cpc columns) into grain-reduced sets."""
    if not path.is_file():
        raise FileNotFoundError(f"portfolio footprint file not found: {path}")
    reader = "read_parquet" if path.suffix.lower() == ".parquet" else "read_csv_auto"
    con = duckdb.connect()
    try:
        cursor = con.execute(f"SELECT * FROM {reader}('{path.as_posix()}')")
        column_count = len(cursor.description)
        rows: list[Any] = cursor.fetchall()
    finally:
        con.close()
    if column_count < 2:
        raise ValueError(f"footprint file {path} needs at least 2 columns (patent, cpc)")
    footprints: dict[str, set[str]] = {}
    for row in rows:
        patent, code = row[0], row[1]
        if not patent or not code:
            continue
        key = _normalize_grant(str(patent))
        footprints.setdefault(key, set()).add(grain_of(str(code), grain))
    return footprints


@dataclass(slots=True)
class MatchReport:
    """Reproducibility record for a match run: both hit rates, thresholds, and counts."""

    buyer_stats: CpcStats
    portfolio_stats: CpcStats
    portfolio_patents: int
    buyer_patents_in: int
    matched_pairs: int
    buyers_out: int
    config_snapshot: dict[str, Any] = field(default_factory=dict[str, Any])


def _overlap_score(
    footprint: set[str], buyer_codes: set[str], metric: OverlapMetric, rarity: dict[str, float]
) -> tuple[float, list[str]]:
    """Score one (portfolio patent × buyer patent) overlap; returns (score, shared grain codes)."""
    shared = sorted(footprint & buyer_codes)
    if not shared:
        return 0.0, shared
    if metric == "jaccard":
        union = footprint | buyer_codes
        return (len(shared) / len(union) if union else 0.0), shared
    if metric == "rarity_weighted":
        return sum(rarity.get(code, 1.0) for code in shared), shared
    return float(len(shared)), shared  # shared_count


def _year_of(date: str | None) -> int:
    """The 4-digit year of a ``YYYYMMDD`` date string, or 0 if absent/short."""
    if date and len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return 0


_BuyerPatents = dict[tuple[str, str], tuple[str, set[str], int]]


def _index_buyer_patents(  # noqa: PLR0913 - flat per-column inputs from the caller
    buyers: list[Any],
    numbers: list[Any],
    kinds: list[Any],
    dates: list[Any],
    code_lists: list[Any],
    grain: CpcGrain,
) -> tuple[_BuyerPatents, CpcStats]:
    """Reduce table rows to distinct (buyer, grant) patents with their grain code set and year."""
    buyer_patents: _BuyerPatents = {}
    grant_found = grant_eligible = 0
    for buyer, number, kind, date, codes in zip(
        buyers, numbers, kinds, dates, code_lists, strict=True
    ):
        pid = normalize_patent_id(number, doc_type_for(number, kind))
        if not pid:
            continue
        grant_eligible += 1
        grain_set = set(reduce_codes(list(codes or []), grain))
        if grain_set:
            grant_found += 1
        key = (str(buyer or ""), pid)
        prev = buyer_patents.get(key)
        year = _year_of(date)
        if prev is None:
            buyer_patents[key] = (str(buyer or ""), grain_set, year)
        else:
            buyer_patents[key] = (prev[0], prev[1] | grain_set, max(prev[2], year))
    stats = CpcStats(
        eligible=grant_eligible, looked_up=grant_eligible, found=grant_found, uncached_offline=0
    )
    return buyer_patents, stats


def _rarity(buyer_patents: _BuyerPatents) -> dict[str, float]:
    """Inverse document frequency of each grain code across buyer patents (for rarity weighting)."""
    doc_freq: dict[str, int] = {}
    for _buyer, grain_set, _year in buyer_patents.values():
        for code in grain_set:
            doc_freq[code] = doc_freq.get(code, 0) + 1
    total = max(len(buyer_patents), 1)
    return {code: total / count for code, count in doc_freq.items()}


def _rank_buyers(  # noqa: PLR0913 - the match knobs are clearer as flat params than bundled
    portfolio_patent: str,
    footprint: set[str],
    buyer_patents: _BuyerPatents,
    rarity: dict[str, float],
    config: CpcMatchConfig,
    *,
    collect_classes: bool = False,
) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
    """Rank buyers for one portfolio patent.

    Returns ``(ranked rows, matched-pair count, class rows)``. ``class rows`` is populated only when
    ``collect_classes`` is set: one row per ``(kept buyer, buyer patent, shared CPC class)`` — the
    many-to-many evidence — emitted for exactly the buyers that survive the ranking filters, so the
    class table never disagrees with the ranked table.
    """
    if not footprint:
        return [], 0, []
    agg: dict[str, dict[str, Any]] = {}
    # buyer -> [(buyer_patent, shared grain codes, year)] — kept only when collect_classes is on.
    evidence: dict[str, list[tuple[str, list[str], int]]] = {}
    matched_pairs = 0
    for (buyer, pid), (_buyer, grain_set, year) in buyer_patents.items():
        score, shared = _overlap_score(footprint, grain_set, config.overlap_metric, rarity)
        if score < config.overlap_threshold or not shared:
            continue
        matched_pairs += 1
        bucket = agg.setdefault(buyer, {"strength": 0.0, "patents": 0, "year": 0, "shared": set()})
        bucket["strength"] += score
        bucket["patents"] += 1
        bucket["year"] = max(bucket["year"], year)
        bucket["shared"].update(shared)
        if collect_classes:
            evidence.setdefault(buyer, []).append((pid, shared, year))
    ranked = [(b, d) for b, d in agg.items() if d["patents"] >= config.min_in_domain_patents]
    for _buyer, data in ranked:
        recency = max(data["year"] - _RECENCY_BASE_YEAR, 0)
        data["rank_score"] = (
            config.weight_overlap * data["strength"]
            + config.weight_volume * data["patents"]
            + config.weight_recency * recency
        )
    ranked.sort(key=lambda item: item[1]["rank_score"], reverse=True)
    rows = [
        {
            "portfolio_patent": portfolio_patent,
            "buyer": buyer,
            "overlap_strength": round(float(data["strength"]), 4),
            "in_domain_patents": int(data["patents"]),
            "last_acquisition_date": _year_str(data["year"]),
            "shared_codes": sorted(data["shared"]),
            "is_off_gazetteer": "true" if buyer.startswith("prov-") else "false",
            "rank_score": round(float(data["rank_score"]), 4),
            "rank": rank,
        }
        for rank, (buyer, data) in enumerate(ranked, start=1)
    ]
    class_rows: list[dict[str, Any]] = []
    if collect_classes:
        for buyer, _data in ranked:  # only buyers that survived min_in_domain_patents
            off_gazetteer = "true" if buyer.startswith("prov-") else "false"
            for pid, shared, year in evidence.get(buyer, []):
                class_rows.extend(
                    {
                        "portfolio_patent": portfolio_patent,
                        "buyer": buyer,
                        "buyer_patent": pid,
                        "cpc_class": code,
                        "year": _year_str(year),
                        "is_off_gazetteer": off_gazetteer,
                    }
                    for code in shared
                )
    return rows, matched_pairs, class_rows


def match_portfolio(  # noqa: PLR0913 - a clear entry point threading the match knobs
    table: pa.Table,
    footprints: dict[str, set[str]],
    *,
    config: CpcMatchConfig,
    buyer_column: str,
    number_column: str,
    kind_column: str,
    date_column: str,
    emit_class_matches: bool = False,
) -> tuple[pa.Table, pa.Table, pa.Table, MatchReport]:
    """Match each portfolio patent's footprint against buyer patents; rank buyers per patent.

    Reads CPC already attached to ``table`` (``cpc_codes`` column, from a prior ``fetch_cpc`` step).
    Returns ``(per_portfolio_patent_table, cross_portfolio_table, class_match_table, report)``. The
    class-match table (one row per portfolio patent × buyer patent × shared CPC class) is only
    populated when ``emit_class_matches`` is set — otherwise it is an empty, correctly-typed table.
    """
    if _CPC_CODES_COLUMN not in table.column_names:
        raise ValueError(
            f"'{_CPC_CODES_COLUMN}' column missing — run a fetch_cpc step before cpc_match"
        )
    grain = config.grain
    buyers = table.column(buyer_column).to_pylist()
    numbers = table.column(number_column).to_pylist()
    kinds = (
        table.column(kind_column).to_pylist()
        if kind_column and kind_column in table.column_names
        else [None] * len(buyers)
    )
    dates = (
        table.column(date_column).to_pylist()
        if date_column and date_column in table.column_names
        else [None] * len(buyers)
    )
    code_lists = table.column(_CPC_CODES_COLUMN).to_pylist()

    buyer_patents, buyer_stats = _index_buyer_patents(
        buyers, numbers, kinds, dates, code_lists, grain
    )
    assert_hit_rate(buyer_stats, config.hit_rate_floor, side="buyer patents")
    rarity = _rarity(buyer_patents)

    per_rows: list[dict[str, Any]] = []
    class_rows: list[dict[str, Any]] = []
    matched_pairs = 0
    for portfolio_patent, footprint in footprints.items():
        rows, pairs, classes = _rank_buyers(
            portfolio_patent,
            footprint,
            buyer_patents,
            rarity,
            config,
            collect_classes=emit_class_matches,
        )
        per_rows.extend(rows)
        class_rows.extend(classes)
        matched_pairs += pairs

    per_table = _rows_to_table(per_rows, _PER_SCHEMA)
    overall_table = _aggregate_overall(per_rows)
    class_table = _rows_to_table(class_rows, _CLASS_SCHEMA)
    report = MatchReport(
        buyer_stats=buyer_stats,
        portfolio_stats=CpcStats(0, 0, 0, 0),  # filled in by the caller (it owns portfolio resolve)
        portfolio_patents=len(footprints),
        buyer_patents_in=len(buyer_patents),
        matched_pairs=matched_pairs,
        buyers_out=len({row["buyer"] for row in per_rows}),
        config_snapshot=config.to_dict(),
    )
    return per_table, overall_table, class_table, report


def _year_str(year: int) -> str:
    return str(year) if year else ""


_PER_SCHEMA = pa.schema(
    [
        pa.field("portfolio_patent", pa.string()),
        pa.field("buyer", pa.string()),
        pa.field("overlap_strength", pa.float64()),
        pa.field("in_domain_patents", pa.int64()),
        pa.field("last_acquisition_date", pa.string()),
        pa.field("shared_codes", pa.list_(pa.string())),
        pa.field("is_off_gazetteer", pa.string()),
        pa.field("rank_score", pa.float64()),
        pa.field("rank", pa.int64()),
    ]
)

_OVERALL_SCHEMA = pa.schema(
    [
        pa.field("buyer", pa.string()),
        pa.field("portfolio_patents_matched", pa.int64()),
        pa.field("total_overlap_strength", pa.float64()),
        pa.field("in_domain_patents", pa.int64()),
        pa.field("last_acquisition_date", pa.string()),
        pa.field("is_off_gazetteer", pa.string()),
    ]
)

# Many-to-many class matches: one row per (portfolio patent × buyer patent × shared CPC class).
_CLASS_SCHEMA = pa.schema(
    [
        pa.field("portfolio_patent", pa.string()),
        pa.field("buyer", pa.string()),
        pa.field("buyer_patent", pa.string()),
        pa.field("cpc_class", pa.string()),  # the shared grain-reduced CPC code
        pa.field("year", pa.string()),  # acquisition year of the buyer patent's deal
        pa.field("is_off_gazetteer", pa.string()),
    ]
)


PER_COLUMNS: list[str] = list(_PER_SCHEMA.names)  # columns of the per-portfolio-patent output
OVERALL_COLUMNS: list[str] = list(_OVERALL_SCHEMA.names)  # columns of the cross-portfolio output
CLASS_MATCH_COLUMNS: list[str] = list(_CLASS_SCHEMA.names)  # columns of the class-match output


def _rows_to_table(rows: list[dict[str, Any]], schema: pa.Schema) -> pa.Table:
    columns = {name: [row[name] for row in rows] for name in schema.names}
    return pa.table(columns, schema=schema)


def _aggregate_overall(per_rows: list[dict[str, Any]]) -> pa.Table:
    """Cross-portfolio view: one row per buyer, how many portfolio patents they match."""
    agg: dict[str, dict[str, Any]] = {}
    for row in per_rows:
        bucket = agg.setdefault(
            row["buyer"],
            {
                "portfolio_patents_matched": 0,
                "total_overlap_strength": 0.0,
                "in_domain_patents": 0,
                "year": 0,
                "is_off_gazetteer": row["is_off_gazetteer"],
            },
        )
        bucket["portfolio_patents_matched"] += 1
        bucket["total_overlap_strength"] += row["overlap_strength"]
        bucket["in_domain_patents"] = max(bucket["in_domain_patents"], row["in_domain_patents"])
        year = _year_of(row["last_acquisition_date"])
        bucket["year"] = max(bucket["year"], year)
    out_rows = [
        {
            "buyer": buyer,
            "portfolio_patents_matched": data["portfolio_patents_matched"],
            "total_overlap_strength": round(float(data["total_overlap_strength"]), 4),
            "in_domain_patents": data["in_domain_patents"],
            "last_acquisition_date": _year_str(data["year"]),
            "is_off_gazetteer": data["is_off_gazetteer"],
        }
        for buyer, data in agg.items()
    ]
    out_rows.sort(
        key=lambda row: (row["portfolio_patents_matched"], row["total_overlap_strength"]),
        reverse=True,
    )
    return _rows_to_table(out_rows, _OVERALL_SCHEMA)
