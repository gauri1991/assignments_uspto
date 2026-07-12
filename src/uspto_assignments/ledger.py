"""The transaction ledger: reconstruct transactions, resolve parties, emit the buyer contracts.

Pipeline (all reads are Parquet/Arrow; DuckDB is the relational layer; **no network calls**):

1. raw tables (from ``ingest``) → conveyance taxonomy (early row cut by *type*, not substring);
2. transaction reconstruction — unit = ``reel_no``+``frame_no``; ``transaction_date`` =
   max assignor ``execution_date``, falling back to ``recorded_date`` (``date_source`` records
   which);
3. every distinct party mention resolved through the dictionary cascade (exact → person → capped
   blocked fuzzy → stable provisional);
4. firm-to-firm predicate **on entities**: every seller and every buyer resolves to an org and the
   seller/buyer ultimate-parent sets are disjoint (drops inventor→employer and intra-group moves);
5. three linked outputs at natural grain, joined on ``entity_id``:
   ``transaction_ledger`` (A), ``buyers`` (B), ``buyer_property_bridge`` (C, the CPC feed);
6. ``reconcile_cpc`` computes the ``cpc_hit_rate`` run metric after an external CPC join —
   a low rate means the id formats are misaligned; fail loudly, never silently.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb as _duckdb
import pyarrow as pa
import pyarrow.parquet as _pq

from .conveyance import DEFAULT_KEPT_TYPES, conveyance_type_column
from .dictionary import append_provisionals, load_dictionary
from .propid import PatentIdFormat, add_doc_columns
from .resolution import ResolvedMention, resolve_mentions

# duckdb/pyarrow.parquet are under-typed; route through Any (see filters.py for the rationale).
duckdb: Any = _duckdb
pq: Any = _pq

OnEvent = Callable[[str], None]

LEDGER_FILE = "transaction_ledger.parquet"
BUYERS_FILE = "buyers.parquet"
BRIDGE_FILE = "buyer_property_bridge.parquet"
METRICS_FILE = "metrics.json"

_RAW_TABLES = ("assignments", "assignors", "assignees", "properties")


def _noop(_message: str) -> None:
    """Default event sink."""


def load_raw(raw_dir: Path) -> dict[str, pa.Table]:
    """Read the four natural-grain Parquet tables written by ``ingest``."""
    tables: dict[str, pa.Table] = {}
    for name in _RAW_TABLES:
        path = raw_dir / f"{name}.parquet"
        if not path.is_file():
            raise FileNotFoundError(f"missing raw table: {path} (run `uspto-assign ingest` first)")
        tables[name] = pq.read_table(path)
    return tables


def _resolution_table(resolved: dict[str, ResolvedMention]) -> pa.Table:
    """The mention→entity resolution as an Arrow table (joinable in DuckDB)."""
    mentions = sorted(resolved)
    return pa.table(
        {
            "mention": mentions,
            "entity_id": [resolved[m].entity_id for m in mentions],
            "canonical_name": [resolved[m].canonical_name for m in mentions],
            "entity_type": [resolved[m].entity_type for m in mentions],
            "resolution_source": [resolved[m].resolution_source for m in mentions],
            "resolution_confidence": pa.array(
                [resolved[m].resolution_confidence for m in mentions], type=pa.float64()
            ),
            "ultimate_parent_id": [resolved[m].ultimate_parent_id for m in mentions],
        }
    )


def resolve_raw_mentions(  # noqa: PLR0913 - the pipeline's tunables, keyword-only
    raw: dict[str, pa.Table],
    dict_dir: Path,
    *,
    threshold: int = 92,
    scorer: str = "token_sort",
    persist_provisionals: bool = True,
    on_event: OnEvent | None = None,
) -> pa.Table:
    """Resolve every distinct assignor/assignee name; returns the resolution table.

    Newly minted provisional ids are persisted back into the dictionary artifact (unless disabled)
    so they stay stable across runs and incremental dumps resolve by exact lookup.
    """
    emit = on_event or _noop
    dictionary = load_dictionary(dict_dir)
    emit(f"dictionary: {dictionary.size()[0]:,} entities, {dictionary.size()[1]:,} aliases")
    names = [
        n
        for table in (raw["assignors"], raw["assignees"])
        for n in table.column("name").to_pylist()
        if n
    ]
    resolved, new_provisionals = resolve_mentions(
        names, dictionary, threshold=threshold, scorer=scorer
    )
    histogram: dict[str, int] = {}
    for mention in resolved.values():
        histogram[mention.resolution_source] = histogram.get(mention.resolution_source, 0) + 1
    emit(f"resolved {len(resolved):,} distinct mentions: {histogram}")
    if persist_provisionals and new_provisionals:
        total = append_provisionals(dict_dir, new_provisionals)
        emit(f"persisted {len(new_provisionals):,} new provisional entities ({total:,} total)")
    return _resolution_table(resolved)


def build_ledger(  # noqa: PLR0913 - the orchestrator threading the pipeline's tunables
    raw_dir: Path,
    dict_dir: Path,
    out_dir: Path,
    *,
    kept_types: frozenset[str] = DEFAULT_KEPT_TYPES,
    threshold: int = 92,
    scorer: str = "token_sort",
    patent_id_format: PatentIdFormat = "patentsview",
    persist_provisionals: bool = True,
    on_event: OnEvent | None = None,
) -> dict[str, Any]:
    """Build the three contract tables from raw Parquet + the dictionary; returns run metrics."""
    emit = on_event or _noop
    raw = load_raw(raw_dir)
    resolution = resolve_raw_mentions(
        raw,
        dict_dir,
        threshold=threshold,
        scorer=scorer,
        persist_provisionals=persist_provisionals,
        on_event=emit,
    )

    assignments = conveyance_type_column(raw["assignments"])
    properties = add_doc_columns(raw["properties"], fmt=patent_id_format)
    assignors, assignees = raw["assignors"], raw["assignees"]

    con: Any = duckdb.connect()
    con.register("assignments", assignments)
    con.register("assignors", assignors)
    con.register("assignees", assignees)
    con.register("properties", properties)
    con.register("resolution", resolution)
    kept_list = ", ".join(f"'{t}'" for t in sorted(kept_types))

    # -- transactions: true date + conveyance type, one row per reel/frame ------------------
    con.execute(
        f"""
        CREATE TEMP TABLE txn AS
        SELECT a.reel_no, a.frame_no, a.conveyance_type, a.conveyance_text AS conveyance_text_raw,
               COALESCE(ex.exec_date, NULLIF(a.recorded_date, '')) AS transaction_date,
               CASE WHEN ex.exec_date IS NOT NULL THEN 'execution'
                    ELSE 'recorded' END AS date_source,
               a.recorded_date, a.correspondent_name
        FROM assignments a
        LEFT JOIN (
            SELECT reel_no, frame_no, MAX(NULLIF(execution_date, '')) AS exec_date
            FROM assignors GROUP BY reel_no, frame_no
        ) ex USING (reel_no, frame_no)
        WHERE a.conveyance_type IN ({kept_list})
        """
    )

    # -- party sides resolved to entities, aggregated per transaction -----------------------
    for side, source in (("seller", "assignors"), ("buyer", "assignees")):
        con.execute(
            f"""
            CREATE TEMP TABLE {side}s_by_txn AS
            SELECT s.reel_no, s.frame_no,
                   list(DISTINCT r.entity_id) AS {side}_entity_ids,
                   list(DISTINCT r.canonical_name) AS {side}_canonical_names,
                   list(DISTINCT r.ultimate_parent_id) AS {side}_parent_ids,
                   bool_and(r.entity_type = 'company') AS {side}_all_company
            FROM {source} s
            JOIN resolution r ON r.mention = s.name
            WHERE s.name IS NOT NULL AND s.name <> ''
            GROUP BY s.reel_no, s.frame_no
            """
        )

    # -- canonical property identity: app number preferred, else grant, else publication ----
    con.execute(
        """
        CREATE TEMP TABLE props AS
        SELECT *,
               FIRST_VALUE(doc_number) OVER (
                   PARTITION BY reel_no, frame_no,
                                COALESCE(NULLIF(invention_title, ''), doc_number)
                   ORDER BY CASE doc_type WHEN 'application' THEN 1 WHEN 'grant' THEN 2
                                          WHEN 'publication' THEN 3 ELSE 4 END,
                            doc_number
               ) AS canonical_property_id
        FROM properties
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE prop_counts AS
        SELECT reel_no, frame_no, COUNT(DISTINCT canonical_property_id) AS property_count
        FROM props GROUP BY reel_no, frame_no
        """
    )

    # -- contract A: firm-to-firm transaction ledger ----------------------------------------
    con.execute(
        """
        CREATE TEMP TABLE ledger AS
        SELECT t.reel_no, t.frame_no, t.conveyance_type, t.conveyance_text_raw,
               t.transaction_date, t.date_source, t.recorded_date,
               s.seller_entity_ids, s.seller_canonical_names,
               b.buyer_entity_ids, b.buyer_canonical_names,
               s.seller_parent_ids, b.buyer_parent_ids,
               COALESCE(p.property_count, 0) AS property_count,
               t.correspondent_name
        FROM txn t
        JOIN sellers_by_txn s USING (reel_no, frame_no)
        JOIN buyers_by_txn b USING (reel_no, frame_no)
        LEFT JOIN prop_counts p USING (reel_no, frame_no)
        WHERE s.seller_all_company AND b.buyer_all_company
          AND NOT list_has_any(s.seller_parent_ids, b.buyer_parent_ids)
        """
    )

    # -- contract C: buyer × canonical property bridge (the CPC feed) -----------------------
    con.execute(
        """
        CREATE TEMP TABLE bridge AS
        SELECT DISTINCT
               u.buyer_entity_id AS entity_id, l.reel_no, l.frame_no,
               pr.canonical_property_id, pr.doc_number AS doc_number_raw,
               pr.doc_kind AS kind_code, pr.doc_type, pr.patent_id_normalized,
               pr.doc_date, pr.invention_title, l.transaction_date,
               '' AS cpc_lookup_status
        FROM ledger l
        JOIN (SELECT reel_no, frame_no, UNNEST(buyer_entity_ids) AS buyer_entity_id FROM ledger) u
             USING (reel_no, frame_no)
        JOIN props pr USING (reel_no, frame_no)
        """
    )

    # -- contract B: one row per resolved buyer entity ---------------------------------------
    con.execute(
        """
        CREATE TEMP TABLE buyers AS
        WITH deals AS (
            SELECT buyer_entity_id AS entity_id,
                   COUNT(*) AS deals_count,
                   MIN(transaction_date) AS first_acquisition_date,
                   MAX(transaction_date) AS last_acquisition_date
            FROM (SELECT reel_no, frame_no, transaction_date,
                         UNNEST(buyer_entity_ids) AS buyer_entity_id FROM ledger)
            GROUP BY buyer_entity_id
        ), patents AS (
            SELECT entity_id, COUNT(DISTINCT canonical_property_id) AS patents_count
            FROM bridge GROUP BY entity_id
        )
        SELECT d.entity_id,
               r.canonical_name, r.entity_type, r.resolution_source, r.resolution_confidence,
               r.ultimate_parent_id,
               COALESCE(pr.canonical_name, r.canonical_name) AS ultimate_parent_name,
               '' AS country, '' AS sector,
               d.deals_count, COALESCE(p.patents_count, 0) AS patents_count,
               d.first_acquisition_date, d.last_acquisition_date,
               (r.entity_id LIKE 'prov-%') AS is_off_gazetteer
        FROM deals d
        JOIN (SELECT DISTINCT ON (entity_id) entity_id, canonical_name, entity_type,
                     resolution_source, resolution_confidence, ultimate_parent_id
              FROM resolution) r USING (entity_id)
        LEFT JOIN (SELECT DISTINCT ON (entity_id) entity_id, canonical_name FROM resolution) pr
               ON pr.entity_id = r.ultimate_parent_id
        LEFT JOIN patents p USING (entity_id)
        """
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    ledger_table: pa.Table = (
        con.execute("SELECT * FROM ledger ORDER BY reel_no, frame_no").arrow().read_all()
    )
    buyers_table: pa.Table = (
        con.execute("SELECT * FROM buyers ORDER BY deals_count DESC").arrow().read_all()
    )
    bridge_table: pa.Table = (
        con.execute("SELECT * FROM bridge ORDER BY entity_id, reel_no, frame_no").arrow().read_all()
    )
    pq.write_table(ledger_table, out_dir / LEDGER_FILE)
    pq.write_table(buyers_table, out_dir / BUYERS_FILE)
    pq.write_table(bridge_table, out_dir / BRIDGE_FILE)

    total_txn: int = con.execute("SELECT count(*) FROM txn").fetchone()[0]
    metrics: dict[str, Any] = {
        "kept_conveyance_types": sorted(kept_types),
        "transactions_considered": total_txn,
        "transactions_firm_to_firm": ledger_table.num_rows,
        "buyers": buyers_table.num_rows,
        "bridge_rows": bridge_table.num_rows,
        "bridge_grant_rows": bridge_table.filter(
            pa.compute.equal(bridge_table.column("doc_type"), "grant")  # type: ignore[attr-defined]
        ).num_rows,
    }
    (out_dir / METRICS_FILE).write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    emit(
        f"ledger: {metrics['transactions_firm_to_firm']:,} firm-to-firm of {total_txn:,} kept txns"
    )
    return metrics


# CPC columns the join adds to the bridge (dropped first on re-run so the join is idempotent).
_CPC_JOIN_COLUMNS = ("cpc_lookup_status", "cpc_codes", "cpc_subclasses")
_CPC_SUBCLASS_LEN = (
    4  # a CPC symbol's subclass is its first 4 chars (e.g. ``H04L9/32`` -> ``H04L``)
)


def reconcile_cpc(
    ledger_dir: Path,
    cpc_path: Path,
    *,
    patent_column: str = "patent_id",
    code_column: str = "cpc_group",
) -> dict[str, Any]:
    """Join the bridge against a CPC table, **attach the CPC codes per patent**, and compute
    ``cpc_hit_rate`` (a fail-loudly reconciliation metric).

    For each bridge grant row the join adds:

    - ``cpc_codes`` — the distinct full CPC symbols for that patent (list; empty if unmatched),
    - ``cpc_subclasses`` — the distinct 4-char subclasses (``H04L`` …) for coarse-grain matching,
    - ``cpc_lookup_status`` — ``found`` / ``not_found`` (grants) or ``na`` (application/publication
      rows, which are not expected to resolve to CPC — route grants only downstream).

    ``code_column`` names the CPC-symbol column in the source (PatentsView ``g_cpc_current`` uses
    ``cpc_group``). The updated bridge is written back; re-running is idempotent.
    """
    bridge = pq.read_table(Path(ledger_dir) / BRIDGE_FILE)
    base_columns = [c for c in bridge.column_names if c not in _CPC_JOIN_COLUMNS]
    con: Any = duckdb.connect()
    con.register("bridge", bridge.select(base_columns))
    cpc_source = str(cpc_path)
    reader = "read_parquet" if cpc_path.suffix.lower() == ".parquet" else "read_csv_auto"
    con.execute(
        f"""
        CREATE TEMP TABLE cpc_agg AS
        SELECT CAST({patent_column} AS VARCHAR) AS pid,
               list(DISTINCT CAST({code_column} AS VARCHAR)) AS cpc_codes,
               list(DISTINCT substr(CAST({code_column} AS VARCHAR), 1, {_CPC_SUBCLASS_LEN}))
                   AS cpc_subclasses
        FROM {reader}('{cpc_source}')
        WHERE {code_column} IS NOT NULL AND CAST({code_column} AS VARCHAR) <> ''
        GROUP BY 1
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE joined AS
        SELECT b.*,
               CASE WHEN b.doc_type <> 'grant' THEN 'na'
                    WHEN a.pid IS NOT NULL THEN 'found'
                    ELSE 'not_found' END AS cpc_lookup_status,
               COALESCE(a.cpc_codes, CAST([] AS VARCHAR[])) AS cpc_codes,
               COALESCE(a.cpc_subclasses, CAST([] AS VARCHAR[])) AS cpc_subclasses
        FROM bridge b
        LEFT JOIN cpc_agg a ON b.patent_id_normalized = a.pid
        """
    )
    updated: pa.Table = con.execute("SELECT * FROM joined").arrow().read_all()
    pq.write_table(updated, Path(ledger_dir) / BRIDGE_FILE)
    eligible: int = con.execute(
        "SELECT count(*) FROM joined WHERE cpc_lookup_status <> 'na'"
    ).fetchone()[0]
    found: int = con.execute(
        "SELECT count(*) FROM joined WHERE cpc_lookup_status = 'found'"
    ).fetchone()[0]
    hit_rate = (found / eligible) if eligible else 0.0
    return {"cpc_eligible_rows": eligible, "cpc_found": found, "cpc_hit_rate": round(hit_rate, 4)}


def top_buyers(
    ledger_dir: Path,
    *,
    by: str = "patents",
    top: int = 20,
    cpc_mode: str = "full",
    sample: int = 25,
) -> pa.Table:
    """Leaderboard query over the materialized outputs (sub-second; no re-run).

    ``by`` = ``patents`` (distinct canonical properties) or ``deals`` (distinct reel/frame).
    ``cpc_mode='sampled'`` restricts each buyer to its ``sample`` most recent **grant** rows —
    the cheap domain-classification pass; ``full`` uses everything.
    """
    con: Any = duckdb.connect()
    buyers = pq.read_table(Path(ledger_dir) / BUYERS_FILE)
    bridge = pq.read_table(Path(ledger_dir) / BRIDGE_FILE)
    con.register("buyers", buyers)
    con.register("bridge", bridge)
    if cpc_mode == "sampled":
        con.execute(
            f"""
            CREATE TEMP TABLE scope AS
            SELECT * FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY entity_id ORDER BY transaction_date DESC
                ) AS rn
                FROM bridge WHERE doc_type = 'grant'
            ) WHERE rn <= {int(sample)}
            """
        )
    else:
        con.execute("CREATE TEMP TABLE scope AS SELECT * FROM bridge")
    metric = "COUNT(DISTINCT s.canonical_property_id)" if by == "patents" else "b.deals_count"
    result: pa.Table = (
        con.execute(
            f"""
        SELECT b.entity_id, b.canonical_name, b.entity_type, b.resolution_source,
               b.is_off_gazetteer, b.deals_count,
               {metric} AS {by}_count,
               b.first_acquisition_date, b.last_acquisition_date
        FROM buyers b LEFT JOIN scope s USING (entity_id)
        GROUP BY ALL ORDER BY {by}_count DESC NULLS LAST LIMIT {int(top)}
        """
        )
        .arrow()
        .read_all()
    )
    return result
