"""Batch processing: run a configurable pipeline over many inputs, without a UI.

A :class:`BatchTemplate` is an ordered pipeline — a load config plus atomic steps
(:class:`FilterStep` / :class:`ExportStep`) — applied to each input independently. Inputs may be
USPTO ``.xml``/``.zip`` files or already-processed dataset folders (Arrow/Parquet). Processing
streams with bounded memory, isolates per-file errors, writes **folder-per-source** outputs
(``<out>/<template>/<source_stem>/<table>.<ext>``), and reports progress through a plain callback
so a UI can mirror it to a console. Templates serialize to JSON like :mod:`uspto_assignments.query`.
"""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict, dataclass, field
from pathlib import Path
from queue import Empty
from typing import Any, Literal

import numpy as np
import pyarrow as pa
import pyarrow.compute as _pc_module

from .classify import ClassifyMethod, classify_column, classify_value
from .classify import CombineMode as ClassifyCombineMode
from .cpcconfig import CPC_CONFIG_FILENAME, load_config
from .cpcmatch import (
    OVERALL_COLUMNS,
    PER_COLUMNS,
    assert_hit_rate,
    attach_cpc,
    load_portfolio_footprint,
    match_portfolio,
)
from .datasource import CpcCache, CpcRunContext, make_source
from .exporters import FORMAT_SUFFIX, ExportFormat, export
from .filters import CombineMode, FilterClause, SortSpec, filter_sort, sort_indices
from .model import columns_for
from .naming import unique_path
from .normalize import (
    DEFAULT_SCORER,
    DEFAULT_THRESHOLD,
    EntityMemory,
    get_scorer,
    normalize_column,
)
from .reference import load_reference, match_column, matched_mask
from .tables import STORE_TABLES, open_dataset, parse_to_store

# pyarrow.compute is under-typed in pyarrow-stubs; route through Any (see filters.py for rationale).
pc: Any = _pc_module

logger = logging.getLogger(__name__)

# The old hard-coded normalize target. Templates saved before target-derivation stored this literal
# for every step (so two steps clobbered one column). Treated as "unset" on load so it derives
# ``{column}_canonical`` — safe because a ``name`` column still derives ``name_canonical``.
LEGACY_NORMALIZE_TARGET = "name_canonical"
# USPTO ``assignor_names``/``assignee_names`` are always joined with this; used to auto-split
# concatenated multi-party columns when a normalize step leaves the separator blank.
_CONCAT_SEPARATOR = "; "


# --------------------------------------------------------------------------------------
# Template model (JSON-serializable)
# --------------------------------------------------------------------------------------
@dataclass(slots=True)
class LoadConfig:
    """How each input is loaded: an optional record cap and per-table field selection."""

    limit: int | None = None
    columns: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    def to_dict(self) -> dict[str, Any]:
        return {"limit": self.limit, "columns": self.columns}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LoadConfig:
        columns = {str(k): [str(c) for c in v] for k, v in data.get("columns", {}).items()}
        limit = data.get("limit")
        return cls(limit=int(limit) if limit is not None else None, columns=columns)


@dataclass(slots=True)
class FilterStep:
    """Transform one table in place: filter rows, optionally project columns and sort."""

    table: str
    clauses: list[FilterClause] = field(default_factory=list[FilterClause])
    combine: CombineMode = "and"
    columns: list[str] | None = None
    sort: SortSpec | None = None
    enabled: bool = True  # disable in the UI to skip a step without deleting it

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "filter",
            "table": self.table,
            "clauses": [asdict(c) for c in self.clauses],
            "combine": self.combine,
            "columns": self.columns,
            "sort": [self.sort[0], self.sort[1]] if self.sort is not None else None,
        }


@dataclass(slots=True)
class ExportStep:
    """Write the current working tables (all, or a named subset) in one format.

    ``columns`` optionally restricts and **reorders** the output columns per table (absent table =
    all columns); ``renames`` maps a source column to an output name per table. Both let you choose
    the final columns produced by earlier steps (canonical/type/matched/disambiguated/id).
    """

    fmt: ExportFormat = "parquet"
    tables: list[str] | None = None
    columns: dict[str, list[str]] | None = None
    renames: dict[str, dict[str, str]] | None = None
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "export",
            "fmt": self.fmt,
            "tables": self.tables,
            "columns": self.columns,
            "renames": self.renames,
        }


@dataclass(slots=True)
class NormalizeStep:
    """Add a fuzzy-normalized canonical column for a name column in one table."""

    table: str
    column: str = "name"
    target: str = ""  # empty -> derived {column}_canonical (so steps never clobber each other)
    threshold: int = DEFAULT_THRESHOLD
    separator: str = ""  # set (e.g. "; ") to normalize each part of a concatenated column
    learn: bool = True  # False = match a curated memory without adding new canonicals
    scorer: str = DEFAULT_SCORER  # rapidfuzz algorithm (see normalize.scorer_names())
    enabled: bool = True

    def resolved_target(self) -> str:
        """The output column name (derived from ``column`` when ``target`` is blank)."""
        return self.target or f"{self.column}_canonical"

    def effective_separator(self) -> str:
        """The split separator, defaulting to ``"; "`` for concatenated ``*_names`` columns."""
        if self.separator:
            return self.separator
        return _CONCAT_SEPARATOR if self.column.endswith("_names") else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "normalize",
            "table": self.table,
            "column": self.column,
            "target": self.target,
            "threshold": self.threshold,
            "separator": self.separator,
            "learn": self.learn,
            "scorer": self.scorer,
        }


@dataclass(slots=True)
class DedupeStep:
    """Drop duplicate rows from a table (keep first), optionally keyed by a column subset."""

    table: str
    subset: list[str] | None = None
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "dedupe", "table": self.table, "subset": self.subset}


@dataclass(slots=True)
class SelectStep:
    """Keep (and reorder) a chosen set of columns in a table."""

    table: str
    columns: list[str] = field(default_factory=list[str])
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "select", "table": self.table, "columns": self.columns}


@dataclass(slots=True)
class SortStep:
    """Order a table by a column."""

    table: str
    column: str = ""
    ascending: bool = True
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "sort",
            "table": self.table,
            "column": self.column,
            "ascending": self.ascending,
        }


@dataclass(slots=True)
class DeriveStep:
    """Add a computed column: year/month of a date, a split part, or a case change."""

    table: str
    source: str
    target: str = ""  # empty -> derived {source}_{op}
    op: str = "year"  # year | month | split_first | upper | lower
    enabled: bool = True

    def resolved_target(self) -> str:
        return self.target or f"{self.source}_{self.op}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "derive",
            "table": self.table,
            "source": self.source,
            "target": self.target,
            "op": self.op,
        }


@dataclass(slots=True)
class AggregateStep:
    """Group a table by columns and count rows into a new summary table (for analysis)."""

    table: str
    group_by: list[str] = field(default_factory=list[str])
    count_distinct: str | None = None
    out_table: str = ""
    enabled: bool = True

    def resolved_out(self) -> str:
        return self.out_table or f"{self.table}_by_{'_'.join(self.group_by) or 'group'}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "aggregate",
            "table": self.table,
            "group_by": self.group_by,
            "count_distinct": self.count_distinct,
            "out_table": self.out_table,
        }


@dataclass(slots=True)
class ClassifyStep:
    """Add an entity-type column (company / individual / unknown) for a name column."""

    table: str
    column: str = "name"
    target: str = ""  # empty -> derived {column}_type
    method: ClassifyMethod = "rules"
    mode: ClassifyCombineMode = "all"  # how to combine multi-party (concatenated) values
    separator: str = ""  # set (e.g. "; ") to classify each party of a concatenated column
    enabled: bool = True

    def resolved_target(self) -> str:
        return self.target or f"{self.column}_type"

    def effective_separator(self) -> str:
        if self.separator:
            return self.separator
        return _CONCAT_SEPARATOR if self.column.endswith("_names") else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "classify",
            "table": self.table,
            "column": self.column,
            "target": self.target,
            "method": self.method,
            "mode": self.mode,
            "separator": self.separator,
        }


@dataclass(slots=True)
class CompareStep:
    """Compare two columns row-wise (e.g. assignor vs assignee); flag or drop matches."""

    table: str
    left: str
    right: str
    target: str = ""  # empty -> derived {left}_matches_{right}
    method: str = "exact"  # exact | fuzzy
    scorer: str = DEFAULT_SCORER  # rapidfuzz algorithm when method == "fuzzy"
    threshold: int = DEFAULT_THRESHOLD
    action: str = "flag"  # flag | drop_matches | keep_matches
    enabled: bool = True

    def resolved_target(self) -> str:
        return self.target or f"{self.left}_matches_{self.right}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "compare",
            "table": self.table,
            "left": self.left,
            "right": self.right,
            "target": self.target,
            "method": self.method,
            "scorer": self.scorer,
            "threshold": self.threshold,
            "action": self.action,
        }


@dataclass(slots=True)
class TransferTypeStep:
    """Keep only rows whose assignor/assignee entity types match a chosen pairing (preset)."""

    table: str = "flat"
    assignor_column: str = "assignor_names"
    assignee_column: str = "assignee_names"
    assignor_type: str = "company"
    assignee_type: str = "company"
    method: ClassifyMethod = "rules"
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "transfer_type",
            "table": self.table,
            "assignor_column": self.assignor_column,
            "assignee_column": self.assignee_column,
            "assignor_type": self.assignor_type,
            "assignee_type": self.assignee_type,
            "method": self.method,
        }


@dataclass(slots=True)
class ReferenceMatchStep:
    """Match a name column against an external disambiguated-assignee reference (company gazetteer).

    Fuzzy-matches each raw name against distinct organization names loaded from ``reference_path``;
    a match normalizes the name to its disambiguated form (and captures the entity id). ``action``
    controls what happens next: ``flag`` adds columns; ``keep_matched``/``drop_matched`` filter.
    """

    table: str = "flat"
    column: str = "assignor_names"
    reference_path: str = ""
    name_column: str = "disambig_assignee_organization"
    id_column: str = ""
    target: str = ""  # empty -> derived {column}_disambiguated
    matched_target: str = ""  # empty -> derived {column}_matched
    id_target: str = ""  # empty -> derived {column}_assignee_id
    threshold: int = DEFAULT_THRESHOLD
    scorer: str = DEFAULT_SCORER
    separator: str = ""
    mode: str = "any"  # any | all — how to combine multi-party (concatenated) match flags
    delimiter: str = ""  # explicit reference delimiter (else auto by extension)
    action: str = "flag"  # flag | keep_matched | drop_matched
    enabled: bool = True

    def resolved_target(self) -> str:
        return self.target or f"{self.column}_disambiguated"

    def resolved_matched(self) -> str:
        return self.matched_target or f"{self.column}_matched"

    def resolved_id(self) -> str:
        return self.id_target or f"{self.column}_assignee_id"

    def effective_separator(self) -> str:
        if self.separator:
            return self.separator
        return _CONCAT_SEPARATOR if self.column.endswith("_names") else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "reference_match",
            "table": self.table,
            "column": self.column,
            "reference_path": self.reference_path,
            "name_column": self.name_column,
            "id_column": self.id_column,
            "target": self.target,
            "matched_target": self.matched_target,
            "id_target": self.id_target,
            "threshold": self.threshold,
            "scorer": self.scorer,
            "separator": self.separator,
            "mode": self.mode,
            "delimiter": self.delimiter,
            "action": self.action,
        }


@dataclass(slots=True)
class FetchCpcStep:
    """Enrich a table with CPC codes for its patent-number column (routes to grants first).

    Adds ``cpc_codes`` (full CPC symbols), ``cpc_subclasses`` (4-char grain), and
    ``cpc_lookup_status`` (``na``/``found``/``not_found``/``uncached``). CPC is resolved through the
    project's configured source + cache (see *Settings ▸ CPC data source*); it is **offline by
    default** — misses are only fetched when the network is enabled for the run. This step performs
    an exact join on the normalized grant number — it is not the fuzzy ``reference_match`` step.
    """

    table: str = "flat"
    column: str = "doc_number"  # the patent-number column
    kind_column: str = "doc_kind"  # kind code, used to route to grants (CPC is grant-only)
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "fetch_cpc",
            "table": self.table,
            "column": self.column,
            "kind_column": self.kind_column,
        }


@dataclass(slots=True)
class CpcMatchStep:
    """Match a sales-package portfolio against buyer CPC footprints; emit ranked buyers per patent.

    Reads CPC already attached by a prior ``fetch_cpc`` step, resolves the portfolio per-patent CPC
    footprint (from a patent-number list via the same source/cache, or a pre-built footprint file),
    and writes a per-portfolio-patent ranked-buyer table plus a cross-portfolio summary. All match
    knobs (grain, overlap metric/threshold, ranking weights, hit-rate floor) come from the project's
    CPC config. Aborts if the CPC hit-rate is below the floor (a likely patent-number mismatch).
    """

    table: str = "flat"
    portfolio_mode: str = "patent_list"  # patent_list | footprint_file
    portfolio_path: str = ""
    buyer_column: str = "assignee_names_canonical"
    number_column: str = "doc_number"
    kind_column: str = "doc_kind"
    date_column: str = "transaction_date"
    out_table: str = "matched_buyers_by_portfolio_patent"
    overall_table: str = "matched_buyers_overall"
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "cpc_match",
            "table": self.table,
            "portfolio_mode": self.portfolio_mode,
            "portfolio_path": self.portfolio_path,
            "buyer_column": self.buyer_column,
            "number_column": self.number_column,
            "kind_column": self.kind_column,
            "date_column": self.date_column,
            "out_table": self.out_table,
            "overall_table": self.overall_table,
        }


BatchStep = (
    FilterStep
    | ExportStep
    | NormalizeStep
    | DedupeStep
    | SelectStep
    | SortStep
    | DeriveStep
    | AggregateStep
    | ClassifyStep
    | CompareStep
    | TransferTypeStep
    | ReferenceMatchStep
    | FetchCpcStep
    | CpcMatchStep
)


def _clause_from_dict(data: dict[str, Any]) -> FilterClause:
    return FilterClause(
        column=str(data["column"]),
        op=data["op"],
        value=str(data.get("value", "")),
        value2=str(data.get("value2", "")),
        case_sensitive=bool(data.get("case_sensitive", False)),
    )


def _step_from_dict(data: dict[str, Any]) -> BatchStep:
    """Decode a step and apply its ``enabled`` flag (default True)."""
    step = _decode_step(data)
    step.enabled = bool(data.get("enabled", True))
    return step


def _decode_step(data: dict[str, Any]) -> BatchStep:  # noqa: PLR0911, PLR0912 - one per step kind
    kind = data.get("kind")
    if kind == "export":
        tables = data.get("tables")
        raw_columns = data.get("columns")
        raw_renames = data.get("renames")
        return ExportStep(
            fmt=data.get("fmt", "parquet"),
            tables=[str(t) for t in tables] if tables is not None else None,
            columns=(
                {str(t): [str(c) for c in cols] for t, cols in raw_columns.items()}
                if raw_columns
                else None
            ),
            renames=(
                {str(t): {str(k): str(v) for k, v in m.items()} for t, m in raw_renames.items()}
                if raw_renames
                else None
            ),
        )
    if kind == "normalize":
        # Migrate pre-fix templates: legacy literal target derives per-column instead (no clobber).
        target_raw = str(data.get("target", ""))
        target = "" if target_raw == LEGACY_NORMALIZE_TARGET else target_raw
        return NormalizeStep(
            table=str(data["table"]),
            column=str(data.get("column", "name")),
            target=target,
            threshold=int(data.get("threshold", DEFAULT_THRESHOLD)),
            separator=str(data.get("separator", "")),
            learn=bool(data.get("learn", True)),
            scorer=str(data.get("scorer", DEFAULT_SCORER)),
        )
    if kind == "classify":
        return ClassifyStep(
            table=str(data["table"]),
            column=str(data.get("column", "name")),
            target=str(data.get("target", "")),
            method=data.get("method", "rules"),
            mode=data.get("mode", "all"),
            separator=str(data.get("separator", "")),
        )
    if kind == "compare":
        return CompareStep(
            table=str(data["table"]),
            left=str(data["left"]),
            right=str(data["right"]),
            target=str(data.get("target", "")),
            method=str(data.get("method", "exact")),
            scorer=str(data.get("scorer", DEFAULT_SCORER)),
            threshold=int(data.get("threshold", DEFAULT_THRESHOLD)),
            action=str(data.get("action", "flag")),
        )
    if kind == "transfer_type":
        return TransferTypeStep(
            table=str(data.get("table", "flat")),
            assignor_column=str(data.get("assignor_column", "assignor_names")),
            assignee_column=str(data.get("assignee_column", "assignee_names")),
            assignor_type=str(data.get("assignor_type", "company")),
            assignee_type=str(data.get("assignee_type", "company")),
            method=data.get("method", "rules"),
        )
    if kind == "reference_match":
        return ReferenceMatchStep(
            table=str(data.get("table", "flat")),
            column=str(data.get("column", "assignor_names")),
            reference_path=str(data.get("reference_path", "")),
            name_column=str(data.get("name_column", "disambig_assignee_organization")),
            id_column=str(data.get("id_column", "")),
            target=str(data.get("target", "")),
            matched_target=str(data.get("matched_target", "")),
            id_target=str(data.get("id_target", "")),
            threshold=int(data.get("threshold", DEFAULT_THRESHOLD)),
            scorer=str(data.get("scorer", DEFAULT_SCORER)),
            separator=str(data.get("separator", "")),
            mode=str(data.get("mode", "any")),
            delimiter=str(data.get("delimiter", "")),
            action=str(data.get("action", "flag")),
        )
    if kind == "dedupe":
        subset = data.get("subset")
        return DedupeStep(
            table=str(data["table"]),
            subset=[str(c) for c in subset] if subset else None,
        )
    if kind == "select":
        return SelectStep(
            table=str(data["table"]),
            columns=[str(c) for c in data.get("columns", [])],
        )
    if kind == "sort":
        return SortStep(
            table=str(data["table"]),
            column=str(data.get("column", "")),
            ascending=bool(data.get("ascending", True)),
        )
    if kind == "derive":
        return DeriveStep(
            table=str(data["table"]),
            source=str(data["source"]),
            target=str(data.get("target", "")),
            op=str(data.get("op", "year")),
        )
    if kind == "aggregate":
        cd = data.get("count_distinct")
        return AggregateStep(
            table=str(data["table"]),
            group_by=[str(c) for c in data.get("group_by", [])],
            count_distinct=str(cd) if cd else None,
            out_table=str(data.get("out_table", "")),
        )
    if kind == "fetch_cpc":
        return FetchCpcStep(
            table=str(data.get("table", "flat")),
            column=str(data.get("column", "doc_number")),
            kind_column=str(data.get("kind_column", "doc_kind")),
        )
    if kind == "cpc_match":
        return CpcMatchStep(
            table=str(data.get("table", "flat")),
            portfolio_mode=str(data.get("portfolio_mode", "patent_list")),
            portfolio_path=str(data.get("portfolio_path", "")),
            buyer_column=str(data.get("buyer_column", "assignee_names_canonical")),
            number_column=str(data.get("number_column", "doc_number")),
            kind_column=str(data.get("kind_column", "doc_kind")),
            date_column=str(data.get("date_column", "transaction_date")),
            out_table=str(data.get("out_table", "matched_buyers_by_portfolio_patent")),
            overall_table=str(data.get("overall_table", "matched_buyers_overall")),
        )
    raw_sort = data.get("sort")
    sort: SortSpec | None = (str(raw_sort[0]), bool(raw_sort[1])) if raw_sort else None
    return FilterStep(
        table=str(data["table"]),
        clauses=[_clause_from_dict(c) for c in data.get("clauses", [])],
        combine=data.get("combine", "and"),
        columns=[str(c) for c in data["columns"]] if data.get("columns") else None,
        sort=sort,
    )


@dataclass(slots=True)
class BatchTemplate:
    """A named batch pipeline: a load config and an ordered list of steps."""

    name: str
    load: LoadConfig = field(default_factory=LoadConfig)
    steps: list[BatchStep] = field(default_factory=list[BatchStep])

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "load": self.load.to_dict(),
            "steps": [{**s.to_dict(), "enabled": s.enabled} for s in self.steps],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatchTemplate:
        return cls(
            name=str(data["name"]),
            load=LoadConfig.from_dict(data.get("load", {})),
            steps=[_step_from_dict(s) for s in data.get("steps", [])],
        )


def dump_templates(templates: list[BatchTemplate], path: Path) -> None:
    """Write templates to ``path`` as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [t.to_dict() for t in templates]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def load_templates(path: Path) -> list[BatchTemplate]:
    """Read templates from ``path`` (``[]`` if missing)."""
    if not path.is_file():
        return []
    return [BatchTemplate.from_dict(item) for item in json.loads(path.read_text(encoding="utf-8"))]


# --------------------------------------------------------------------------------------
# Schema propagation + validation (drives schema-aware pickers and pre-run warnings)
# --------------------------------------------------------------------------------------
def columns_after(  # noqa: PLR0912 - one branch per step kind
    load: LoadConfig, steps: Sequence[BatchStep], upto: int
) -> dict[str, list[str]]:
    """The columns present on each table after applying the first ``upto`` (enabled) steps.

    Starts from the loaded base schema (``load.columns`` projection or the full table schema) and
    folds each step's column effect — adds derived/canonical/type/match columns, applies
    filter/select projections, and creates aggregate output tables. Powers schema-aware column
    pickers and validation; disabled steps are ignored (they don't run).
    """
    cols: dict[str, list[str]] = {}
    for table in STORE_TABLES:
        base = load.columns.get(table)
        cols[table] = list(base) if base else list(columns_for(table))

    def add(table: str, name: str) -> None:
        column_list = cols.setdefault(table, [])
        if name and name not in column_list:
            column_list.append(name)

    def project(table: str, chosen: list[str]) -> None:
        existing = cols.get(table, [])
        kept = [c for c in chosen if c in existing]
        cols[table] = kept or list(chosen)

    for step in steps[:upto]:
        if not step.enabled:
            continue
        if isinstance(step, NormalizeStep | ClassifyStep | DeriveStep):
            add(step.table, step.resolved_target())
        elif isinstance(step, CompareStep):
            if step.action == "flag":
                add(step.table, step.resolved_target())
        elif isinstance(step, ReferenceMatchStep):
            add(step.table, step.resolved_target())
            add(step.table, step.resolved_matched())
            if step.id_column:
                add(step.table, step.resolved_id())
        elif isinstance(step, FilterStep):
            if step.columns:
                project(step.table, step.columns)
        elif isinstance(step, SelectStep):
            project(step.table, step.columns)
        elif isinstance(step, AggregateStep):
            out = [*step.group_by, "count"]
            if step.count_distinct:
                out.append(f"{step.count_distinct}_distinct")
            cols[step.resolved_out()] = out
        elif isinstance(step, FetchCpcStep):
            for name in ("cpc_codes", "cpc_subclasses", "cpc_lookup_status"):
                add(step.table, name)
        elif isinstance(step, CpcMatchStep):
            cols[step.out_table] = list(PER_COLUMNS)
            cols[step.overall_table] = list(OVERALL_COLUMNS)
        # Dedupe / Sort / TransferType / Export: columns unchanged
    return cols


def _referenced_columns(step: BatchStep) -> tuple[str, list[str]]:  # noqa: PLR0911 - per step kind
    """The ``(table, columns)`` a single-table step reads as input (for validation)."""
    if isinstance(step, FilterStep):
        refs = [c.column for c in step.clauses] + ([step.sort[0]] if step.sort else [])
        return step.table, refs
    if isinstance(step, NormalizeStep | ClassifyStep | ReferenceMatchStep):
        return step.table, [step.column]
    if isinstance(step, CompareStep):
        return step.table, [step.left, step.right]
    if isinstance(step, DeriveStep):
        return step.table, [step.source]
    if isinstance(step, SortStep):
        return step.table, [step.column]
    if isinstance(step, SelectStep):
        return step.table, list(step.columns)
    if isinstance(step, DedupeStep):
        return step.table, list(step.subset or [])
    if isinstance(step, AggregateStep):
        distinct = [step.count_distinct] if step.count_distinct else []
        return step.table, [*step.group_by, *distinct]
    if isinstance(step, TransferTypeStep):
        return step.table, [step.assignor_column, step.assignee_column]
    if isinstance(step, FetchCpcStep):
        return step.table, [step.column]
    if isinstance(step, CpcMatchStep):
        return step.table, [step.buyer_column, step.number_column, "cpc_codes"]
    return "", []  # ExportStep validated separately


def validate_template(  # noqa: PLR0912 - one validation branch per step kind
    load: LoadConfig, steps: Sequence[BatchStep]
) -> list[str]:
    """Return human-readable warnings about a template (missing columns/tables/reference files)."""
    warnings: list[str] = []
    if not any(s.enabled for s in steps):
        warnings.append("The pipeline has no enabled steps.")
    for index, step in enumerate(steps, start=1):
        if not step.enabled:
            continue
        available = columns_after(load, steps, index - 1)  # columns available as input to this step
        table, refs = _referenced_columns(step)
        if table and table not in available:
            warnings.append(f"Step {index} ({type(step).__name__}): table '{table}' is not loaded.")
        else:
            present = available.get(table, [])
            for column in refs:
                if column and column not in present:
                    warnings.append(
                        f"Step {index} ({type(step).__name__}): "
                        f"column '{column}' is not available on '{table}' yet."
                    )
        if isinstance(step, ReferenceMatchStep):
            if not step.reference_path:
                warnings.append(f"Step {index} (ReferenceMatch): no reference file set.")
            elif not Path(step.reference_path).is_file():
                warnings.append(
                    f"Step {index} (ReferenceMatch): reference file not found: "
                    f"{step.reference_path}"
                )
        if isinstance(step, CpcMatchStep) and step.portfolio_mode == "footprint_file":
            if not step.portfolio_path:
                warnings.append(f"Step {index} (CpcMatch): no portfolio footprint file set.")
            elif not Path(step.portfolio_path).is_file():
                warnings.append(
                    f"Step {index} (CpcMatch): portfolio file not found: {step.portfolio_path}"
                )
        if isinstance(step, CpcMatchStep) and step.portfolio_mode == "patent_list":
            if not step.portfolio_path:
                warnings.append(f"Step {index} (CpcMatch): no portfolio patent-list file set.")
            elif not Path(step.portfolio_path).is_file():
                warnings.append(
                    f"Step {index} (CpcMatch): portfolio file not found: {step.portfolio_path}"
                )
    return warnings


# --------------------------------------------------------------------------------------
# Events + results
# --------------------------------------------------------------------------------------
@dataclass(slots=True)
class BatchEvent:
    """A progress/log event surfaced to the caller (mirrored to the UI console)."""

    level: str  # "info" | "error" | "success"
    message: str
    # "file_done" marks a per-file completion line — the UI drives its determinate progress bar
    # from this, not from the message text.
    kind: Literal["message", "file_done"] = "message"


@dataclass(slots=True)
class FileResult:
    """The outcome of processing one input file."""

    source: str
    ok: bool
    outputs: list[str] = field(default_factory=list[str])
    rows: dict[str, int] = field(default_factory=dict[str, int])
    error: str | None = None
    elapsed: float = 0.0  # wall-clock seconds spent on this file
    learned: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])  # normalization


@dataclass(slots=True)
class BatchResult:
    """The aggregate outcome of a batch run."""

    succeeded: int
    failed: int
    results: list[FileResult]
    cancelled: bool = False  # True when the run stopped early via ``should_stop``


OnEvent = Callable[[BatchEvent], None]


def _noop_event(_event: BatchEvent) -> None:
    """No-op event sink used when the caller passes no ``on_event``."""


def _never_stop() -> bool:
    """Default ``should_stop``: never cancel."""
    return False


OnParse = Callable[[int], None]
# Report parse progress this often (records), and throttle the combined parallel line this often.
_PARSE_PROGRESS_INTERVAL = 500
_COMBINED_EMIT_SECONDS = 0.3


# --------------------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------------------
def _safe_name(name: str) -> str:
    cleaned = "".join(c if (c.isalnum() or c in " -_") else "_" for c in name).strip()
    return cleaned or "batch"


def _needed_tables(template: BatchTemplate) -> set[str] | None:
    """Return the minimal set of tables a template touches, or None if all are needed.

    Lets the parser skip building unused tables — most importantly the wide ``flat`` table.
    """
    for step in template.steps:
        if isinstance(step, ExportStep) and step.tables is None:
            return None  # an export writes every table
    needed: set[str] = {name for name, cols in template.load.columns.items() if cols}
    for step in template.steps:
        if isinstance(
            step,
            FilterStep
            | NormalizeStep
            | DedupeStep
            | SelectStep
            | SortStep
            | DeriveStep
            | AggregateStep
            | ClassifyStep
            | CompareStep
            | TransferTypeStep
            | ReferenceMatchStep
            | FetchCpcStep
            | CpcMatchStep,
        ):
            needed.add(step.table)  # every non-export step reads/writes a named source table
        elif step.tables is not None:  # ExportStep by elimination
            needed.update(step.tables)
    return needed or None


def _load_tables(
    template: BatchTemplate, source: Path, work_dir: Path, on_parse: OnParse | None
) -> dict[str, pa.Table]:
    if source.is_dir():
        store = open_dataset(source)
    else:
        store = parse_to_store(
            source,
            work_dir,
            limit=template.load.limit,
            tables=_needed_tables(template),
            progress=on_parse,
            progress_interval=_PARSE_PROGRESS_INTERVAL,
        )
    if template.load.columns:
        store = store.select_columns(template.load.columns)
    return dict(store.tables)


def _unique_target(step: NormalizeStep, used: set[tuple[str, str]], emit: OnEvent) -> str:
    """The step's target, disambiguated if another step already wrote it to the same table.

    Targets collide only within one table, so ``used`` is keyed by ``(table, target)``.
    """
    target = step.resolved_target()
    if (step.table, target) not in used:
        return target
    base = f"{step.column}_canonical"  # fall back to a column-derived name
    candidate = base
    suffix = 2
    while (step.table, candidate) in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    emit(BatchEvent("info", f"  note: '{target}' already written — using '{candidate}' instead"))
    return candidate


def _apply_normalize(
    tables: dict[str, pa.Table],
    step: NormalizeStep,
    memory: EntityMemory,
    used_targets: set[tuple[str, str]],
    emit: OnEvent,
) -> None:
    table = tables.get(step.table)
    if table is None or step.column not in table.column_names:
        emit(BatchEvent("info", f"  skip normalize: {step.table}.{step.column} not present"))
        return
    target = _unique_target(step, used_targets, emit)
    used_targets.add((step.table, target))

    def on_progress(done: int, total: int) -> None:
        emit(
            BatchEvent(
                "info", f"  normalizing {step.table}.{step.column}: {done:,} of {total:,} names"
            )
        )

    tables[step.table] = normalize_column(
        table,
        step.column,
        target,
        memory,
        threshold=step.threshold,
        separator=step.effective_separator(),
        learn=step.learn,
        scorer=step.scorer,
        on_progress=on_progress,
    )


def _apply_classify(tables: dict[str, pa.Table], step: ClassifyStep, emit: OnEvent) -> None:
    table = tables.get(step.table)
    if table is None or step.column not in table.column_names:
        emit(BatchEvent("info", f"  skip classify: {step.table}.{step.column} not present"))
        return

    def on_progress(done: int, total: int) -> None:
        emit(
            BatchEvent(
                "info", f"  classifying {step.table}.{step.column}: {done:,} of {total:,} names"
            )
        )

    tables[step.table] = classify_column(
        table,
        step.column,
        step.resolved_target(),
        method=step.method,
        separator=step.effective_separator(),
        mode=step.mode,
        on_progress=on_progress,
    )
    emit(BatchEvent("info", f"  classify {step.table}.{step.column} → {step.resolved_target()}"))


def _match_mask(table: pa.Table, step: CompareStep) -> Any:
    """A boolean array: True where ``left`` matches ``right`` (exact, or fuzzy ≥ threshold)."""
    left: Any = pc.cast(table.column(step.left), pa.string())
    right: Any = pc.cast(table.column(step.right), pa.string())
    if step.method != "fuzzy":
        return pc.fill_null(pc.equal(left, right), False)
    scorer_fn = get_scorer(step.scorer)
    left_vals = left.to_pylist()
    right_vals = right.to_pylist()
    flags = [
        a is not None and b is not None and scorer_fn(a, b) >= step.threshold
        for a, b in zip(left_vals, right_vals, strict=True)
    ]
    return pa.array(flags, type=pa.bool_())


def _apply_compare(tables: dict[str, pa.Table], step: CompareStep, emit: OnEvent) -> None:
    table = tables.get(step.table)
    if table is None or step.left not in table.column_names or step.right not in table.column_names:
        emit(
            BatchEvent("info", f"  skip compare: {step.table}.{step.left}/{step.right} not present")
        )
        return
    mask: Any = _match_mask(table, step)
    match_count = pc.sum(pc.cast(mask, pa.int64())).as_py() or 0
    if step.action == "drop_matches":
        tables[step.table] = table.filter(pc.invert(mask))
        emit(BatchEvent("info", f"  compare {step.table}: dropped {match_count:,} matching rows"))
    elif step.action == "keep_matches":
        tables[step.table] = table.filter(mask)
        emit(BatchEvent("info", f"  compare {step.table}: kept {match_count:,} matching rows"))
    else:  # flag: add a "true"/"false" column the existing filters can act on
        target = step.resolved_target()
        flags: Any = pc.if_else(mask, pa.scalar("true"), pa.scalar("false"))
        if target in table.column_names:
            table = table.drop_columns([target])
        tables[step.table] = table.append_column(target, flags)
        emit(BatchEvent("info", f"  compare {step.table} → {target} ({match_count:,} matches)"))


def _apply_transfer_type(
    tables: dict[str, pa.Table], step: TransferTypeStep, emit: OnEvent
) -> None:
    table = tables.get(step.table)
    cols = table.column_names if table is not None else []
    if table is None or step.assignor_column not in cols or step.assignee_column not in cols:
        emit(BatchEvent("info", f"  skip transfer-type: {step.table} columns not present"))
        return
    before = table.num_rows

    def type_mask(column: str, wanted: str) -> Any:
        values = pc.cast(table.column(column), pa.string()).to_pylist()
        sep = _CONCAT_SEPARATOR if column.endswith("_names") else ""
        flags = [
            v is not None
            and classify_value(v, method=step.method, separator=sep, mode="all") == wanted
            for v in values
        ]
        return pa.array(flags, type=pa.bool_())

    mask: Any = pc.and_(
        type_mask(step.assignor_column, step.assignor_type),
        type_mask(step.assignee_column, step.assignee_type),
    )
    tables[step.table] = table.filter(mask)
    kept = tables[step.table].num_rows
    emit(
        BatchEvent(
            "info",
            f"  transfer-type {step.assignor_type}→{step.assignee_type}: "
            f"{before:,} → {kept:,} rows",
        )
    )


def _apply_reference_match(
    tables: dict[str, pa.Table], step: ReferenceMatchStep, emit: OnEvent
) -> None:
    table = tables.get(step.table)
    if table is None or step.column not in table.column_names:
        emit(BatchEvent("info", f"  skip reference-match: {step.table}.{step.column} not present"))
        return
    if not step.reference_path or not Path(step.reference_path).is_file():
        emit(
            BatchEvent("info", f"  skip reference-match: reference '{step.reference_path}' missing")
        )
        return

    gazetteer = load_reference(
        Path(step.reference_path),
        step.name_column,
        id_column=step.id_column,
        delimiter=step.delimiter,
    )
    emit(BatchEvent("info", f"  reference: {gazetteer.size():,} disambiguated organizations"))

    def on_progress(done: int, total: int) -> None:
        emit(BatchEvent("info", f"  matching {step.table}.{step.column}: {done:,} of {total:,}"))

    matched_col = step.resolved_matched()
    result = match_column(
        table,
        step.column,
        gazetteer,
        step.resolved_target(),
        matched_col,
        step.resolved_id() if step.id_column else "",
        threshold=step.threshold,
        scorer=step.scorer,
        separator=step.effective_separator(),
        mode=step.mode,
        on_progress=on_progress,
    )
    mask: Any = matched_mask(result, matched_col)
    hits = pc.sum(pc.cast(mask, pa.int64())).as_py() or 0
    if step.action == "keep_matched":
        result = result.filter(mask)
    elif step.action == "drop_matched":
        result = result.filter(pc.invert(mask))
    tables[step.table] = result
    emit(
        BatchEvent(
            "info",
            f"  reference-match {step.table}.{step.column}: {hits:,} of {table.num_rows:,} "
            f"matched → {result.num_rows:,} rows ({step.action})",
        )
    )


def _combined_key(table: pa.Table, columns: list[str]) -> Any:
    """A single string array joining ``columns`` per row (nulls → "") for keying/grouping."""
    parts: list[Any] = [pc.cast(table.column(c), pa.string()) for c in columns]
    return pc.binary_join_element_wise(*parts, "\x1f", null_handling="replace", null_replacement="")


def _apply_dedupe(tables: dict[str, pa.Table], step: DedupeStep, emit: OnEvent) -> None:
    table = tables.get(step.table)
    if table is None:
        emit(BatchEvent("info", f"  skip dedupe: table '{step.table}' not present"))
        return
    cols = step.subset or list(table.column_names)
    cols = [c for c in cols if c in table.column_names]
    if not cols:
        return
    before = table.num_rows
    encoded: Any = _combined_key(table, cols).combine_chunks().dictionary_encode()
    codes: Any = encoded.indices.to_numpy(zero_copy_only=False)
    _, first_index = np.unique(codes, return_index=True)  # first occurrence of each distinct key
    keep = np.sort(first_index)
    keep_arr: Any = pa.array(keep)
    tables[step.table] = table.take(keep_arr)
    emit(BatchEvent("info", f"  dedupe {step.table}: {before:,} → {len(keep):,} rows"))


def _apply_select(tables: dict[str, pa.Table], step: SelectStep, emit: OnEvent) -> None:
    table = tables.get(step.table)
    if table is None:
        emit(BatchEvent("info", f"  skip select: table '{step.table}' not present"))
        return
    keep = [c for c in step.columns if c in table.column_names]
    if not keep:
        emit(BatchEvent("info", f"  skip select: no matching columns in '{step.table}'"))
        return
    tables[step.table] = table.select(keep)
    emit(BatchEvent("info", f"  select {step.table}: kept {len(keep)} column(s)"))


def _apply_sort(tables: dict[str, pa.Table], step: SortStep, emit: OnEvent) -> None:
    table = tables.get(step.table)
    if table is None or step.column not in table.column_names:
        emit(BatchEvent("info", f"  skip sort: {step.table}.{step.column} not present"))
        return
    indices = sort_indices(table, step.column, ascending=step.ascending)
    tables[step.table] = table.take(indices)
    order = "asc" if step.ascending else "desc"
    emit(BatchEvent("info", f"  sort {step.table} by {step.column} ({order})"))


def _derive_array(source: Any, op: str) -> Any:
    """Compute a derived string array from ``source`` for a supported ``op``."""
    if op == "year":
        return pc.utf8_slice_codeunits(source, 0, 4)
    if op == "month":
        return pc.utf8_slice_codeunits(source, 4, 6)
    if op == "upper":
        return pc.utf8_upper(source)
    if op == "lower":
        return pc.utf8_lower(source)
    if op == "split_first":
        return pc.list_element(pc.split_pattern(source, pattern="; "), 0)
    raise ValueError(f"unknown derive op: {op!r}")


def _apply_derive(tables: dict[str, pa.Table], step: DeriveStep, emit: OnEvent) -> None:
    table = tables.get(step.table)
    if table is None or step.source not in table.column_names:
        emit(BatchEvent("info", f"  skip derive: {step.table}.{step.source} not present"))
        return
    source: Any = pc.cast(table.column(step.source), pa.string())
    derived = _derive_array(source, step.op)
    target = step.resolved_target()
    if target in table.column_names:
        table = table.drop_columns([target])
    tables[step.table] = table.append_column(target, derived)
    emit(BatchEvent("info", f"  derive {step.table}.{target} = {step.op}({step.source})"))


def _apply_aggregate(tables: dict[str, pa.Table], step: AggregateStep, emit: OnEvent) -> None:
    table = tables.get(step.table)
    if table is None:
        emit(BatchEvent("info", f"  skip aggregate: table '{step.table}' not present"))
        return
    keys = [c for c in step.group_by if c in table.column_names]
    if not keys:
        emit(BatchEvent("info", f"  skip aggregate: no group-by columns in '{step.table}'"))
        return
    aggs: list[Any] = [([], "count_all")]
    if step.count_distinct and step.count_distinct in table.column_names:
        aggs.append(([step.count_distinct], "count_distinct"))
    grouped: Any = table.group_by(keys).aggregate(aggs)
    grouped = grouped.rename_columns(
        [*keys, "count", *([f"{step.count_distinct}_distinct"] if len(aggs) > 1 else [])]
    )
    order = sort_indices(grouped, "count", ascending=False)
    out = step.resolved_out()
    tables[out] = grouped.take(order)
    emit(
        BatchEvent(
            "info",
            f"  aggregate {step.table} by {', '.join(keys)} → "
            f"{out} ({tables[out].num_rows:,} groups)",
        )
    )


def _apply_filter(tables: dict[str, pa.Table], step: FilterStep, emit: OnEvent) -> None:
    table = tables.get(step.table)
    if table is None:
        emit(BatchEvent("info", f"  skip filter: table '{step.table}' not present"))
        return
    before = table.num_rows
    indices = filter_sort(table, step.clauses, combine=step.combine, sort=step.sort)
    result = table.take(indices)
    if step.columns:
        keep = [c for c in step.columns if c in result.column_names]
        if keep:
            result = result.select(keep)
    tables[step.table] = result
    emit(BatchEvent("info", f"  filter {step.table}: {before:,} → {result.num_rows:,} rows"))


def _project_for_export(table: pa.Table, step: ExportStep, name: str) -> pa.Table:
    """Apply the export step's per-table column selection/order and renames to ``table``."""
    chosen = (step.columns or {}).get(name)
    if chosen:  # keep only the chosen columns that exist, in the chosen order
        keep = [c for c in chosen if c in table.column_names]
        if keep:
            table = table.select(keep)
    renames = (step.renames or {}).get(name)
    if renames:
        table = table.rename_columns([renames.get(c, c) for c in table.column_names])
    return table


def _apply_export(
    tables: dict[str, pa.Table], step: ExportStep, source_dir: Path, emit: OnEvent
) -> list[str]:
    # Default export order: the known store tables first, then any derived/aggregate tables.
    extras = [n for n in tables if n not in STORE_TABLES]
    names = step.tables or [n for n in STORE_TABLES if n in tables] + extras
    written: list[str] = []
    for name in names:
        table = tables.get(name)
        if table is None:
            continue

        table = _project_for_export(table, step, name)
        path = unique_path(source_dir / f"{name}{FORMAT_SUFFIX[step.fmt]}")
        export(table, path, step.fmt)
        written.append(str(path))
        emit(BatchEvent("info", f"  export {name} → {path.name} ({table.num_rows:,} rows)"))
    return written


def _resolve_cpc_ctx(cpc_ctx: CpcRunContext | None) -> CpcRunContext:
    """Return the run's CPC context, loading the default project config if none was supplied."""
    if cpc_ctx is not None:
        return cpc_ctx
    return CpcRunContext(config=load_config(Path(CPC_CONFIG_FILENAME)), allow_network=False)


def _apply_fetch_cpc(
    tables: dict[str, pa.Table], step: FetchCpcStep, ctx: CpcRunContext, emit: OnEvent
) -> None:
    table = tables.get(step.table)
    if table is None:
        emit(BatchEvent("info", f"  skip fetch_cpc: table '{step.table}' not present"))
        return
    if step.column not in table.column_names:
        emit(BatchEvent("info", f"  skip fetch_cpc: column '{step.column}' not in '{step.table}'"))
        return
    cache = CpcCache(ctx.config, make_source(ctx.config))
    out, stats = attach_cpc(
        table,
        number_column=step.column,
        kind_column=step.kind_column,
        cache=cache,
        allow_network=ctx.allow_network,
    )
    tables[step.table] = out
    emit(
        BatchEvent(
            "info",
            f"  fetch_cpc {step.table}.{step.column}: {stats.found:,}/{stats.eligible:,} grants "
            f"resolved (hit-rate {stats.hit_rate:.0%})",
        )
    )
    if stats.uncached_offline:
        emit(
            BatchEvent(
                "info",
                f"  note: {stats.uncached_offline:,} grant patents are uncached — enable network "
                f"for this run to fetch their CPC codes",
            )
        )


def _apply_cpc_match(
    tables: dict[str, pa.Table], step: CpcMatchStep, ctx: CpcRunContext, emit: OnEvent
) -> None:
    table = tables.get(step.table)
    if table is None:
        emit(BatchEvent("info", f"  skip cpc_match: table '{step.table}' not present"))
        return
    match_cfg = ctx.config.match
    cache = CpcCache(ctx.config, make_source(ctx.config))
    footprints, pstats = load_portfolio_footprint(
        mode=step.portfolio_mode,
        path=Path(step.portfolio_path),
        grain=match_cfg.grain,
        cache=cache,
        allow_network=ctx.allow_network,
    )
    if step.portfolio_mode == "patent_list":
        assert_hit_rate(pstats, match_cfg.hit_rate_floor, side="portfolio patents")
    per, overall, report = match_portfolio(
        table,
        footprints,
        config=match_cfg,
        buyer_column=step.buyer_column,
        number_column=step.number_column,
        kind_column=step.kind_column,
        date_column=step.date_column,
    )
    tables[step.out_table] = per
    tables[step.overall_table] = overall
    emit(
        BatchEvent(
            "info",
            f"  cpc_match: {report.portfolio_patents} portfolio patent(s), buyer hit-rate "
            f"{report.buyer_stats.hit_rate:.0%}; {report.matched_pairs} matched pair(s) → "
            f"{per.num_rows:,} ranked rows across {report.buyers_out} buyer(s)",
        )
    )


def _apply_step(  # noqa: PLR0912, PLR0913 - one branch per step kind
    tables: dict[str, pa.Table],
    step: BatchStep,
    memory: EntityMemory,
    used_targets: set[tuple[str, str]],
    source_dir: Path,
    emit: OnEvent,
    cpc_ctx: CpcRunContext | None = None,
) -> list[str]:
    """Apply one step in place; returns written paths (only an ExportStep writes anything)."""
    if isinstance(step, FilterStep):
        _apply_filter(tables, step, emit)
    elif isinstance(step, NormalizeStep):
        _apply_normalize(tables, step, memory, used_targets, emit)
    elif isinstance(step, DedupeStep):
        _apply_dedupe(tables, step, emit)
    elif isinstance(step, SelectStep):
        _apply_select(tables, step, emit)
    elif isinstance(step, SortStep):
        _apply_sort(tables, step, emit)
    elif isinstance(step, DeriveStep):
        _apply_derive(tables, step, emit)
    elif isinstance(step, AggregateStep):
        _apply_aggregate(tables, step, emit)
    elif isinstance(step, ClassifyStep):
        _apply_classify(tables, step, emit)
    elif isinstance(step, CompareStep):
        _apply_compare(tables, step, emit)
    elif isinstance(step, TransferTypeStep):
        _apply_transfer_type(tables, step, emit)
    elif isinstance(step, ReferenceMatchStep):
        _apply_reference_match(tables, step, emit)
    elif isinstance(step, FetchCpcStep):
        _apply_fetch_cpc(tables, step, _resolve_cpc_ctx(cpc_ctx), emit)
    elif isinstance(step, CpcMatchStep):
        _apply_cpc_match(tables, step, _resolve_cpc_ctx(cpc_ctx), emit)
    else:
        return _apply_export(tables, step, source_dir, emit)
    return []


_LARGE_DROP_FRACTION = 0.99  # warn (info) when a step removes at least this share of a table's rows


def _warn_if_emptied(  # noqa: PLR0913 - a small guard threading the loop's locals
    tables: dict[str, pa.Table],
    step: BatchStep,
    index: int,
    table_name: str,
    before: int | None,
    emit: OnEvent,
) -> None:
    """Surface the #1 silent failure: a step that drops a non-empty table to 0 (or nearly)."""
    if not before or not table_name:
        return
    after = tables[table_name].num_rows if table_name in tables else 0
    kind = type(step).__name__.removesuffix("Step")
    if after == 0:
        emit(
            BatchEvent(
                "error",
                f"  ⚠ step {index} ({kind}) left '{table_name}' EMPTY — check the "
                f"filter clause, reference_path/name_column, or the match gate",
            )
        )
    elif after <= before * (1 - _LARGE_DROP_FRACTION):
        emit(
            BatchEvent(
                "info",
                f"  note: step {index} ({kind}) dropped {before - after:,} of {before:,} rows "
                f"from '{table_name}'",
            )
        )


def _process_input(  # noqa: PLR0913 - threads collaborators; one branch per step kind
    template: BatchTemplate,
    source: Path,
    template_dir: Path,
    source_dir: Path,
    emit: OnEvent,
    on_parse: OnParse | None = None,
    memory: EntityMemory | None = None,
    cpc_ctx: CpcRunContext | None = None,
) -> FileResult:
    memory = memory if memory is not None else EntityMemory()
    learned_before = len(memory.learned)
    start = time.monotonic()
    work_dir: Path | None = None
    try:
        if source.is_file():
            work_dir = Path(tempfile.mkdtemp(prefix="uspto_batch_"))
        tables = _load_tables(template, source, work_dir or template_dir, on_parse)
        source_dir.mkdir(parents=True, exist_ok=True)
        outputs: list[str] = []
        used_targets: set[tuple[str, str]] = set()  # (table, target) claimed (collision guard)
        for index, step in enumerate(template.steps, start=1):
            if not step.enabled:
                continue  # UI-disabled step: skip without deleting
            table_name: str = getattr(step, "table", "")
            before = tables[table_name].num_rows if table_name in tables else None
            outputs.extend(
                _apply_step(tables, step, memory, used_targets, source_dir, emit, cpc_ctx)
            )
            _warn_if_emptied(tables, step, index, table_name, before, emit)
        rows = {name: table.num_rows for name, table in tables.items()}
        result = FileResult(
            str(source),
            ok=True,
            outputs=outputs,
            rows=rows,
            learned=memory.learned[learned_before:],
        )
    except Exception as exc:  # per-file isolation: log, record, and keep going
        logger.exception("batch: failed on %s", source)
        result = FileResult(str(source), ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
    result.elapsed = time.monotonic() - start
    return result


def _process_one(  # noqa: PLR0913 - picklable worker threading the run's collaborators
    template: BatchTemplate,
    source: Path,
    template_dir: Path,
    source_dir: Path,
    event_queue: Any,
    memory: EntityMemory,
    cpc_ctx: CpcRunContext | None = None,
) -> FileResult:
    """Picklable process-pool worker: streams events/progress live via ``event_queue``.

    The start line carries the worker PID so distinct interleaving PIDs prove real parallelism;
    ``("parse", source, count)`` items drive per-file progress + a combined total in the parent.
    """
    start = BatchEvent("info", f"▶ {source.name} (worker pid {os.getpid()})")
    event_queue.put(("event", str(source), start))
    return _process_input(
        template,
        source,
        template_dir,
        source_dir,
        emit=lambda event: event_queue.put(("event", str(source), event)),
        on_parse=lambda count: event_queue.put(("parse", str(source), count)),
        memory=memory,
        cpc_ctx=cpc_ctx,
    )


def _assign_source_dirs(inputs: Sequence[Path], template_dir: Path) -> list[Path]:
    """Reserve one distinct output dir per input, serially, before any processing starts.

    ``unique_path`` alone is race-prone across worker processes: two same-stem inputs (e.g.
    ``a/x.xml`` and ``b/x.xml``) would both see the same free path and overwrite each other's
    outputs. Deciding every dir up front in the parent keeps uniqueness serial; ``claimed`` tracks
    dirs reserved this run that don't exist on disk yet.
    """
    claimed: set[Path] = set()
    dirs: list[Path] = []
    for source in inputs:
        candidate = template_dir / source.stem
        counter = 1
        while candidate in claimed or candidate.exists():
            candidate = template_dir / f"{source.stem} ({counter})"
            counter += 1
        claimed.add(candidate)
        dirs.append(candidate)
    return dirs


def _future_result(future: Future[FileResult], source: Path) -> FileResult:
    """Collect one worker future, converting an unexpected crash into a failed :class:`FileResult`.

    ``_process_input`` already isolates per-file errors, so an exception here means the worker
    process itself died or its result couldn't be unpickled. ``BrokenProcessPool`` is re-raised —
    it poisons the whole pool and the caller must stop the run.
    """
    try:
        return future.result()
    except BrokenProcessPool:
        raise
    except Exception as exc:  # per-file isolation, mirroring the sequential path
        return FileResult(str(source), ok=False, error=f"{type(exc).__name__}: {exc}")


def _record_pool_failure(
    exc: BrokenProcessPool, inputs: list[Path], results: list[FileResult], emit: OnEvent
) -> None:
    """Record every not-yet-collected input as failed after the pool died."""
    message = f"{type(exc).__name__}: worker process died — remaining inputs skipped"
    emit(BatchEvent("error", message))
    collected = {result.source for result in results}
    results.extend(
        FileResult(str(src), ok=False, error=message) for src in inputs if str(src) not in collected
    )


def _cancel_pending(pending: set[Future[FileResult]], emit: OnEvent) -> set[Future[FileResult]]:
    """Drop every not-yet-started future; return the ones already in flight."""
    remaining = {future for future in pending if not future.cancel()}
    if remaining:
        emit(BatchEvent("info", "Batch cancelled — waiting for in-flight file(s)…"))
    return remaining


def _run_parallel(  # noqa: PLR0913 - internal helper threading the run's collaborators
    template: BatchTemplate,
    inputs: list[Path],
    source_dirs: Sequence[Path],
    template_dir: Path,
    workers: int,
    emit: OnEvent,
    memory: EntityMemory,
    cpc_ctx: CpcRunContext | None = None,
    should_stop: Callable[[], bool] = _never_stop,
) -> tuple[list[FileResult], bool]:
    """Process inputs across a worker pool, emitting each file's events plus a combined total.

    Returns ``(results, cancelled)``. Cancellation is per-file: not-yet-started inputs are
    dropped, files already in flight run to completion and are collected normally.
    """
    results: list[FileResult] = []
    per_source: dict[str, int] = {}
    completed = 0
    total = len(inputs)
    last_emit = 0.0

    with mp.Manager() as manager:
        event_queue: Any = manager.Queue()

        def drain(*, force: bool) -> None:
            nonlocal last_emit
            updated = False
            while True:
                try:
                    kind, source, payload = event_queue.get_nowait()
                except Empty:
                    break
                name = Path(source).name
                if kind == "parse":
                    per_source[source] = payload
                    updated = True
                    emit(BatchEvent("info", f"[{name}] parsing… {payload:,} assignments"))
                else:
                    emit(
                        BatchEvent(
                            payload.level, f"[{name}] {payload.message.strip()}", kind=payload.kind
                        )
                    )
            now = time.monotonic()
            if per_source and (force or (updated and now - last_emit >= _COMBINED_EMIT_SECONDS)):
                grand_total = sum(per_source.values())
                emit(
                    BatchEvent(
                        "info",
                        f"  processing… {grand_total:,} records across "
                        f"{len(per_source)} file(s), {completed}/{total} done",
                    )
                )
                last_emit = now

        with ProcessPoolExecutor(max_workers=workers) as pool:
            # ``memory`` is pickled once per submit (per input file). Acceptable at current sizes;
            # an ``initializer``-based one-time transfer per worker is the upgrade path if this
            # ever shows up in profiles.
            futures = {
                pool.submit(
                    _process_one, template, src, template_dir, src_dir, event_queue, memory, cpc_ctx
                ): src
                for src, src_dir in zip(inputs, source_dirs, strict=True)
            }
            pending = set(futures)
            cancelled = False
            try:
                while pending:
                    if not cancelled and should_stop():
                        cancelled = True
                        pending = _cancel_pending(pending, emit)
                    drain(force=False)
                    finished = {future for future in pending if future.done()}
                    pending -= finished
                    for future in finished:
                        result = _future_result(future, futures[future])
                        _emit_file_done(result, emit)
                        results.append(result)
                        completed += 1
                    if pending:
                        time.sleep(0.05)
            except BrokenProcessPool as exc:
                # The pool is unusable; record the remaining inputs as failed and stop.
                _record_pool_failure(exc, inputs, results, emit)
            drain(force=True)
    order = {str(path): index for index, path in enumerate(inputs)}
    return sorted(results, key=lambda r: order.get(r.source, len(order))), cancelled


def _emit_file_done(result: FileResult, emit: OnEvent) -> None:
    took = f" ({result.elapsed:.1f}s)"
    if result.ok:
        emit(BatchEvent("success", f"✓ {Path(result.source).name} done{took}", kind="file_done"))
    else:
        emit(
            BatchEvent(
                "error", f"✗ {Path(result.source).name}: {result.error}{took}", kind="file_done"
            )
        )


def _write_run_log(
    template_dir: Path, timestamp: str, template: BatchTemplate, results: list[FileResult]
) -> None:
    log_path = template_dir / f"run_{timestamp or 'batch'}.log"
    lines = [f"Batch template: {template.name}", f"Timestamp: {timestamp}", ""]
    for result in results:
        status = "OK" if result.ok else f"FAILED: {result.error}"
        lines.append(
            f"{result.source}: {status}  {result.elapsed:.1f}s  "
            f"rows={result.rows} outputs={len(result.outputs)}"
        )
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_batch(  # noqa: PLR0913 - a clear public entry point with keyword-only options
    template: BatchTemplate,
    inputs: list[Path],
    out_root: Path,
    *,
    workers: int = 1,
    timestamp: str = "",
    memory: EntityMemory | None = None,
    on_event: OnEvent | None = None,
    cpc_ctx: CpcRunContext | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> BatchResult:
    """Run ``template`` over ``inputs``, writing outputs under ``out_root``.

    Args:
        template: The pipeline to apply to each input.
        inputs: Files (``.xml``/``.zip``) and/or dataset folders to process.
        out_root: Root output directory; a ``<template-name>/`` subfolder is created inside it.
        workers: Number of parallel worker processes (``1`` = sequential with live per-step events).
        timestamp: Stamp used in the run-log filename (the caller supplies it).
        memory: Entity memory for any normalize steps; new aliases learned during the run are
            merged back into it (persist it afterwards to keep learning). A fresh one is used if
            ``None``.
        on_event: Optional callback receiving :class:`BatchEvent`s as processing proceeds.
        should_stop: Optional cancellation probe, polled between files (cancellation is per-file:
            the file in flight finishes, remaining inputs are skipped). Must be thread-safe.

    Returns:
        A :class:`BatchResult` summarizing successes, failures, and per-file details;
        ``result.cancelled`` is True when the run stopped early.
    """
    emit = on_event or _noop_event
    stop = should_stop or _never_stop
    memory = memory if memory is not None else EntityMemory()
    run_start = time.monotonic()
    template_dir = out_root / _safe_name(template.name)
    template_dir.mkdir(parents=True, exist_ok=True)
    source_dirs = _assign_source_dirs(inputs, template_dir)
    results: list[FileResult] = []
    cancelled = False

    if workers > 1 and len(inputs) > 1:
        emit(BatchEvent("info", f"Processing {len(inputs)} inputs with {workers} workers…"))
        results, cancelled = _run_parallel(
            template,
            inputs,
            source_dirs,
            template_dir,
            workers,
            emit,
            memory,
            cpc_ctx,
            should_stop=stop,
        )
        # Workers mutate pickled copies of the memory; merge what they learned back in. (The
        # sequential path shares ``memory`` in-process, so its learned pairs are already there.)
        for result in results:
            memory.apply_learned(result.learned)
    else:
        for source, source_dir in zip(inputs, source_dirs, strict=True):
            if stop():
                cancelled = True
                emit(BatchEvent("info", "Batch cancelled — skipping remaining inputs"))
                break
            emit(BatchEvent("info", f"▶ {source.name}"))
            result = _process_input(
                template,
                source,
                template_dir,
                source_dir,
                emit,
                on_parse=lambda count: emit(
                    BatchEvent("info", f"  parsing… {count:,} assignments")
                ),
                memory=memory,
                cpc_ctx=cpc_ctx,
            )
            _emit_file_done(result, emit)
            results.append(result)

    _write_run_log(template_dir, timestamp, template, results)
    succeeded = sum(1 for r in results if r.ok)
    failed = len(results) - succeeded
    elapsed = time.monotonic() - run_start
    if cancelled:
        skipped = len(inputs) - len(results)
        emit(
            BatchEvent(
                "error" if failed else "info",
                f"Batch cancelled: {succeeded} succeeded, {failed} failed, "
                f"{skipped} skipped in {elapsed:.1f}s",
            )
        )
    else:
        emit(
            BatchEvent(
                "success" if failed == 0 else "error",
                f"Batch complete: {succeeded} succeeded, {failed} failed in {elapsed:.1f}s",
            )
        )
    return BatchResult(succeeded=succeeded, failed=failed, results=results, cancelled=cancelled)


# --------------------------------------------------------------------------------------
# Preview (dry-run on a small sample)
# --------------------------------------------------------------------------------------
@dataclass(slots=True)
class StepStat:
    """Per-step preview stats: how the working table changed when the step ran."""

    index: int  # 1-based position in the template
    label: str  # human-readable step summary
    table: str
    rows_before: int
    rows_after: int
    columns_added: list[str] = field(default_factory=list[str])
    note: str = ""


PREVIEW_LIMIT = 1000


def _default_step_label(step: BatchStep) -> str:
    return type(step).__name__


def run_preview(  # noqa: PLR0913 - a clear public entry point with keyword-only options
    template: BatchTemplate,
    source: Path,
    *,
    limit: int = PREVIEW_LIMIT,
    describe: Callable[[BatchStep], str] | None = None,
    on_event: OnEvent | None = None,
    cpc_ctx: CpcRunContext | None = None,
) -> tuple[dict[str, pa.Table], list[StepStat]]:
    """Run ``template`` on ``source`` capped to ``limit`` records; return tables + per-step stats.

    A fast dry-run for the UI: it applies the same steps as a real run but **skips Export** and
    keeps the working tables in memory, recording each step's row change and added columns so the
    caller can show "the data as of each step". Uses a fresh entity memory (not persisted).
    """
    emit = on_event or _noop_event
    label_of = describe if describe is not None else _default_step_label
    preview = BatchTemplate(
        name=template.name,
        load=LoadConfig(
            limit=min(limit, template.load.limit or limit), columns=template.load.columns
        ),
        steps=template.steps,
    )
    memory = EntityMemory()
    used_targets: set[tuple[str, str]] = set()
    stats: list[StepStat] = []
    work_dir: Path | None = None
    try:
        if source.is_file():
            work_dir = Path(tempfile.mkdtemp(prefix="uspto_preview_"))
        tables = _load_tables(preview, source, work_dir or source.parent, None)
        for index, step in enumerate(template.steps, start=1):
            table_name: str = getattr(step, "table", "")
            before_rows = tables[table_name].num_rows if table_name in tables else 0
            before_cols: set[str] = (
                set(tables[table_name].column_names) if table_name in tables else set()
            )
            before_tables = set(tables)
            if not step.enabled:
                stats.append(
                    StepStat(
                        index, label_of(step), table_name, before_rows, before_rows, note="disabled"
                    )
                )
                continue
            if isinstance(step, ExportStep):
                stats.append(
                    StepStat(
                        index,
                        label_of(step),
                        table_name,
                        before_rows,
                        before_rows,
                        note="export (skipped in preview)",
                    )
                )
                continue
            _apply_step(
                tables, step, memory, used_targets, work_dir or source.parent, emit, cpc_ctx
            )
            after_rows = tables[table_name].num_rows if table_name in tables else before_rows
            after_cols: set[str] = (
                set(tables[table_name].column_names) if table_name in tables else set()
            )
            added = sorted(after_cols - before_cols)
            new_tables = sorted(set(tables) - before_tables)
            note = f"+table {', '.join(new_tables)}" if new_tables else ""
            if after_rows == 0 < before_rows:  # the #1 silent failure — make it loud in the preview
                note = "⚠ dropped all rows"
            stats.append(
                StepStat(index, label_of(step), table_name, before_rows, after_rows, added, note)
            )
    finally:
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
    return tables, stats
