"""Batch processing: run a configurable pipeline over many inputs, without a UI.

A :class:`BatchTemplate` is an ordered pipeline — a load config plus atomic steps
(:class:`FilterStep` / :class:`ExportStep`) — applied to each input independently. Inputs may be
USPTO ``.xml``/``.zip`` files or already-processed dataset folders (Arrow/Parquet). Processing
streams with bounded memory, isolates per-file errors, writes **folder-per-source** outputs
(``<out>/<template>/<source_stem>/<table>.<ext>``), and reports progress through a plain callback
so a UI can mirror it to a console. Templates serialize to JSON like :mod:`uspto_assignments.query`.
"""

from __future__ import annotations

import csv
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
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty
from typing import Any, Literal, cast

import numpy as np
import pyarrow as pa
import pyarrow.compute as _pc_module

from .classify import ClassifyMethod, classify_column, classify_value, probablepeople_available
from .classify import CombineMode as ClassifyCombineMode
from .cpcconfig import CPC_CONFIG_FILENAME, load_config
from .cpcmatch import (
    CLASS_MATCH_COLUMNS,
    OVERALL_COLUMNS,
    PER_COLUMNS,
    assert_hit_rate,
    attach_cpc,
    attach_cpc_from_file,
    load_portfolio_footprint,
    match_portfolio,
)
from .datasource import CpcCache, CpcRunContext, make_source
from .exporters import FORMAT_SUFFIX, ExportFormat, export, write_workbook
from .filters import CombineMode, FilterClause, SortSpec, filter_sort, sort_indices
from .model import columns_for
from .namemodel import model_available
from .naming import unique_path
from .normalize import (
    DEFAULT_SCORER,
    DEFAULT_THRESHOLD,
    EntityMemory,
    get_scorer,
    normalize_column,
    scorer_names,
)
from .reference import load_reference, match_column, matched_mask, reference_columns
from .tables import STORE_TABLES, dataset_columns, open_dataset, parse_to_store

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
        raw_limit = data.get("limit")
        # ``0`` (and any non-positive value) means "no cap", matching the UI's spinbox where
        # 0 = all; a literal small positive cap is the only meaningful non-null value.
        limit = int(raw_limit) if raw_limit is not None else None
        if limit is not None and limit <= 0:
            limit = None
        return cls(limit=limit, columns=columns)


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
    emit_score: bool = False  # add a {target}_score column (weakest part-confidence, 0–100)
    review_threshold: int = 0  # >0: add {target}_review flagging fuzzy accepts scoring below it
    emit_type: bool = False  # add a {target}_type column from the memory's stored entity tags
    enabled: bool = True

    def resolved_target(self) -> str:
        """The output column name (derived from ``column`` when ``target`` is blank)."""
        return self.target or f"{self.column}_canonical"

    def resolved_score(self) -> str:
        """The confidence column name (added only when ``emit_score`` is set)."""
        return f"{self.resolved_target()}_score"

    def resolved_review(self) -> str:
        """The review-flag column name (added only when ``review_threshold`` > 0)."""
        return f"{self.resolved_target()}_review"

    def resolved_type(self) -> str:
        """The entity-type column name (added only when ``emit_type`` is set)."""
        return f"{self.resolved_target()}_type"

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
            "emit_score": self.emit_score,
            "review_threshold": self.review_threshold,
            "emit_type": self.emit_type,
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
    emit_score: bool = False  # add a {target}_score column (per-row similarity, 0–100)
    review_threshold: int = 0  # >0: add {target}_review flagging fuzzy matches scoring below it
    enabled: bool = True

    def resolved_target(self) -> str:
        return self.target or f"{self.left}_matches_{self.right}"

    def resolved_score(self) -> str:
        """The confidence column name (added only when ``emit_score`` is set)."""
        return f"{self.resolved_target()}_score"

    def resolved_review(self) -> str:
        """The review-flag column name (added only when ``review_threshold`` > 0)."""
        return f"{self.resolved_target()}_review"

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
            "emit_score": self.emit_score,
            "review_threshold": self.review_threshold,
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
    emit_score: bool = False  # add a {column}_match_score column (weakest matched-part score)
    review_threshold: int = 0  # >0: add {column}_match_review flagging accepts scoring below it
    enabled: bool = True

    def resolved_target(self) -> str:
        return self.target or f"{self.column}_disambiguated"

    def resolved_matched(self) -> str:
        return self.matched_target or f"{self.column}_matched"

    def resolved_id(self) -> str:
        return self.id_target or f"{self.column}_assignee_id"

    def resolved_score(self) -> str:
        """The confidence column name (added only when ``emit_score`` is set)."""
        return f"{self.column}_match_score"

    def resolved_review(self) -> str:
        """The review-flag column name (added only when ``review_threshold`` > 0)."""
        return f"{self.column}_match_review"

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
            "emit_score": self.emit_score,
            "review_threshold": self.review_threshold,
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
class AttachCpcFileStep:
    """Attach CPC codes from an uploaded file (PatSeer/CSV/Parquet) instead of the API.

    Same output columns as :class:`FetchCpcStep` (``cpc_codes``/``cpc_subclasses``/
    ``cpc_lookup_status``) but the CPC comes from ``source_path`` joined on ``patent_column``, with
    the symbols in ``code_column``. ``separator`` splits multi-code cells (blank = one code per
    row). Fully offline — no network, no cache. Ideal after shortlisting records.
    """

    table: str = "flat"
    column: str = "doc_number"  # the table's patent-number column
    kind_column: str = "doc_kind"  # kind code, routes to grants (CPC is grant-only)
    source_path: str = ""  # the uploaded PatSeer/CSV/Parquet export
    patent_column: str = "Publication Number"  # the file's patent-number column
    code_column: str = "CPC"  # the file's CPC-symbol column
    separator: str = ";"  # split multi-code cells (blank = one code per row)
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "attach_cpc_file",
            "table": self.table,
            "column": self.column,
            "kind_column": self.kind_column,
            "source_path": self.source_path,
            "patent_column": self.patent_column,
            "code_column": self.code_column,
            "separator": self.separator,
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
    emit_class_matches: bool = False  # also emit the per-class match table (many-to-many evidence)
    class_match_table: str = "matched_cpc_classes"
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
            "emit_class_matches": self.emit_class_matches,
            "class_match_table": self.class_match_table,
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
    | AttachCpcFileStep
    | CpcMatchStep
)


class TemplateFormatError(ValueError):
    """A template file/object is malformed: bad shape, unknown step kind, or invalid enum value.

    Raised with a message that names the offending template, step, and field so an author can fix
    the JSON — the import path (UI and CLI) shows it verbatim instead of a bare traceback.
    """


# The closed enum vocabularies of the template format (templateInfo.md §4). Values outside these
# used to be silently coerced to defaults at run time — an imported typo like ``combine: "AND"``
# or ``action: "drop"`` would run "successfully" with the opposite semantics.
_FILTER_OPS = frozenset(
    {"contains", "equals", "not_equals", "starts_with", "not_empty", "is_empty", "in_range"}
)
_COMBINE_MODES = frozenset({"and", "or"})
_DERIVE_OPS = frozenset({"year", "month", "split_first", "upper", "lower"})
_COMPARE_METHODS = frozenset({"exact", "fuzzy"})
_COMPARE_ACTIONS = frozenset({"flag", "drop_matches", "keep_matches"})
_CLASSIFY_METHODS = frozenset({"rules", "probablepeople", "model"})
_CLASSIFY_MODES = frozenset({"all", "any", "first", "majority"})
_REFERENCE_MODES = frozenset({"any", "all"})
_REFERENCE_ACTIONS = frozenset({"flag", "keep_matched", "drop_matched"})
_ENTITY_TYPES = frozenset({"company", "individual", "unknown"})
_PORTFOLIO_MODES = frozenset({"patent_list", "footprint_file"})


def _expect_enum(
    data: dict[str, Any], field_name: str, allowed: frozenset[str], default: str
) -> str:
    value = str(data.get(field_name, default) or default)
    if value not in allowed:
        raise TemplateFormatError(
            f"invalid {field_name} {value!r} (allowed: {', '.join(sorted(allowed))})"
        )
    return value


def _expect_scorer(data: dict[str, Any]) -> str:
    value = str(data.get("scorer", DEFAULT_SCORER) or DEFAULT_SCORER)
    if value not in scorer_names():
        raise TemplateFormatError(
            f"invalid scorer {value!r} (allowed: {', '.join(scorer_names())})"
        )
    return value


def _expect_threshold(data: dict[str, Any], field_name: str, default: int) -> int:
    raw = data.get(field_name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise TemplateFormatError(f"{field_name} must be an integer, got {raw!r}") from exc
    if not 0 <= value <= 100:
        raise TemplateFormatError(f"{field_name} must be between 0 and 100, got {value}")
    return value


def _expect_bool(data: dict[str, Any], field_name: str, default: bool) -> bool:
    raw = data.get(field_name, default)
    if not isinstance(raw, bool):
        # bool("false") is True — a string here always means the author got the opposite of
        # what they wrote, so reject it instead of guessing.
        raise TemplateFormatError(f"{field_name} must be true or false, got {raw!r}")
    return raw


def _require(data: dict[str, Any], key: str, kind: str) -> str:
    if key not in data:
        raise TemplateFormatError(f"{kind} step is missing required field '{key}'")
    return str(data[key])


def _expect_str_list(raw: Any, field_name: str) -> list[str]:
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise TemplateFormatError(f"{field_name} must be a list of column names, got {raw!r}")
    return [str(c) for c in cast("list[Any]", raw)]


def _clause_from_dict(raw: Any) -> FilterClause:
    if not isinstance(raw, dict):
        raise TemplateFormatError(f"each filter clause must be an object, got {raw!r}")
    data = cast(dict[str, Any], raw)
    return FilterClause(
        column=_require(data, "column", "filter clause"),
        op=_expect_enum(data, "op", _FILTER_OPS, "contains"),  # type: ignore[arg-type]
        value=str(data.get("value", "")),
        value2=str(data.get("value2", "")),
        case_sensitive=_expect_bool(data, "case_sensitive", False),
    )


def _step_from_dict(data: dict[str, Any]) -> BatchStep:
    """Decode a step and apply its ``enabled`` flag (default True)."""
    step = _decode_step(data)
    step.enabled = _expect_bool(data, "enabled", True)
    return step


def _decode_step(data: dict[str, Any]) -> BatchStep:  # noqa: PLR0911, PLR0912 - one per step kind
    kind = data.get("kind")
    if kind == "export":
        tables = data.get("tables")
        raw_columns = data.get("columns")
        raw_renames = data.get("renames")
        fmt = str(data.get("fmt", "parquet"))
        if fmt not in FORMAT_SUFFIX:
            raise TemplateFormatError(
                f"invalid fmt {fmt!r} (allowed: {', '.join(sorted(FORMAT_SUFFIX))})"
            )
        return ExportStep(
            fmt=fmt,  # type: ignore[arg-type]  # membership-checked against FORMAT_SUFFIX
            tables=_expect_str_list(tables, "tables") if tables is not None else None,
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
            table=_require(data, "table", "normalize"),
            column=str(data.get("column", "name")),
            target=target,
            threshold=_expect_threshold(data, "threshold", DEFAULT_THRESHOLD),
            separator=str(data.get("separator", "")),
            learn=_expect_bool(data, "learn", True),
            scorer=_expect_scorer(data),
            emit_score=_expect_bool(data, "emit_score", False),
            review_threshold=_expect_threshold(data, "review_threshold", 0),
            emit_type=_expect_bool(data, "emit_type", False),
        )
    if kind == "classify":
        return ClassifyStep(
            table=_require(data, "table", "classify"),
            column=str(data.get("column", "name")),
            target=str(data.get("target", "")),
            method=_expect_enum(data, "method", _CLASSIFY_METHODS, "rules"),  # type: ignore[arg-type]
            mode=_expect_enum(data, "mode", _CLASSIFY_MODES, "all"),  # type: ignore[arg-type]
            separator=str(data.get("separator", "")),
        )
    if kind == "compare":
        return CompareStep(
            table=_require(data, "table", "compare"),
            left=_require(data, "left", "compare"),
            right=_require(data, "right", "compare"),
            target=str(data.get("target", "")),
            method=_expect_enum(data, "method", _COMPARE_METHODS, "exact"),
            scorer=_expect_scorer(data),
            threshold=_expect_threshold(data, "threshold", DEFAULT_THRESHOLD),
            action=_expect_enum(data, "action", _COMPARE_ACTIONS, "flag"),
            emit_score=_expect_bool(data, "emit_score", False),
            review_threshold=_expect_threshold(data, "review_threshold", 0),
        )
    if kind == "transfer_type":
        return TransferTypeStep(
            table=str(data.get("table", "flat")),
            assignor_column=str(data.get("assignor_column", "assignor_names")),
            assignee_column=str(data.get("assignee_column", "assignee_names")),
            assignor_type=_expect_enum(data, "assignor_type", _ENTITY_TYPES, "company"),
            assignee_type=_expect_enum(data, "assignee_type", _ENTITY_TYPES, "company"),
            method=_expect_enum(data, "method", _CLASSIFY_METHODS, "rules"),  # type: ignore[arg-type]
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
            threshold=_expect_threshold(data, "threshold", DEFAULT_THRESHOLD),
            scorer=_expect_scorer(data),
            separator=str(data.get("separator", "")),
            mode=_expect_enum(data, "mode", _REFERENCE_MODES, "any"),
            delimiter=str(data.get("delimiter", "")),
            action=_expect_enum(data, "action", _REFERENCE_ACTIONS, "flag"),
            emit_score=_expect_bool(data, "emit_score", False),
            review_threshold=_expect_threshold(data, "review_threshold", 0),
        )
    if kind == "dedupe":
        subset = data.get("subset")
        return DedupeStep(
            table=_require(data, "table", "dedupe"),
            subset=_expect_str_list(subset, "subset") if subset else None,
        )
    if kind == "select":
        return SelectStep(
            table=_require(data, "table", "select"),
            columns=_expect_str_list(data.get("columns", []), "columns"),
        )
    if kind == "sort":
        return SortStep(
            table=_require(data, "table", "sort"),
            column=str(data.get("column", "")),
            ascending=_expect_bool(data, "ascending", True),
        )
    if kind == "derive":
        return DeriveStep(
            table=_require(data, "table", "derive"),
            source=_require(data, "source", "derive"),
            target=str(data.get("target", "")),
            op=_expect_enum(data, "op", _DERIVE_OPS, "year"),
        )
    if kind == "aggregate":
        cd = data.get("count_distinct")
        return AggregateStep(
            table=_require(data, "table", "aggregate"),
            group_by=_expect_str_list(data.get("group_by", []), "group_by"),
            count_distinct=str(cd) if cd else None,
            out_table=str(data.get("out_table", "")),
        )
    if kind == "fetch_cpc":
        return FetchCpcStep(
            table=str(data.get("table", "flat")),
            column=str(data.get("column", "doc_number")),
            kind_column=str(data.get("kind_column", "doc_kind")),
        )
    if kind == "attach_cpc_file":
        return AttachCpcFileStep(
            table=str(data.get("table", "flat")),
            column=str(data.get("column", "doc_number")),
            kind_column=str(data.get("kind_column", "doc_kind")),
            source_path=str(data.get("source_path", "")),
            patent_column=str(data.get("patent_column", "Publication Number")),
            code_column=str(data.get("code_column", "CPC")),
            separator=str(data.get("separator", ";")),
        )
    if kind == "cpc_match":
        return CpcMatchStep(
            table=str(data.get("table", "flat")),
            portfolio_mode=_expect_enum(data, "portfolio_mode", _PORTFOLIO_MODES, "patent_list"),
            portfolio_path=str(data.get("portfolio_path", "")),
            buyer_column=str(data.get("buyer_column", "assignee_names_canonical")),
            number_column=str(data.get("number_column", "doc_number")),
            kind_column=str(data.get("kind_column", "doc_kind")),
            date_column=str(data.get("date_column", "transaction_date")),
            out_table=str(data.get("out_table", "matched_buyers_by_portfolio_patent")),
            overall_table=str(data.get("overall_table", "matched_buyers_overall")),
            emit_class_matches=bool(data.get("emit_class_matches", False)),
            class_match_table=str(data.get("class_match_table", "matched_cpc_classes")),
        )
    if kind == "filter":
        raw_sort: Any = data.get("sort")
        if raw_sort is not None and (
            isinstance(raw_sort, str)
            or not isinstance(raw_sort, (list, tuple))
            or len(cast("list[Any]", raw_sort)) != 2
        ):
            raise TemplateFormatError(
                f'sort must be ["<column>", <ascending bool>] or null, got {raw_sort!r}'
            )
        sort_pair = cast("list[Any] | None", raw_sort)
        sort: SortSpec | None = (str(sort_pair[0]), bool(sort_pair[1])) if sort_pair else None
        raw_clauses: Any = data.get("clauses", [])
        if isinstance(raw_clauses, (str, dict)) or not isinstance(raw_clauses, list):
            raise TemplateFormatError(
                f"clauses must be a list of clause objects, got {raw_clauses!r}"
            )
        return FilterStep(
            table=_require(data, "table", "filter"),
            clauses=[_clause_from_dict(c) for c in cast("list[Any]", raw_clauses)],
            combine=_expect_enum(data, "combine", _COMBINE_MODES, "and"),  # type: ignore[arg-type]
            columns=_expect_str_list(data["columns"], "columns") if data.get("columns") else None,
            sort=sort,
        )
    raise TemplateFormatError(
        f"unknown step kind {kind!r} — valid kinds: filter, normalize, classify, compare, "
        f"transfer_type, reference_match, fetch_cpc, attach_cpc_file, cpc_match, dedupe, "
        f"select, sort, derive, aggregate, export"
    )


@dataclass(slots=True)
class BatchTemplate:
    """A named batch pipeline: a load config and an ordered list of steps.

    ``description`` is optional free-text help that travels *with* the template through
    import/save/export — so a user-authored or imported template can document itself. Shipped
    templates leave it blank and are documented in the UI's curated help instead.
    """

    name: str
    load: LoadConfig = field(default_factory=LoadConfig)
    steps: list[BatchStep] = field(default_factory=list[BatchStep])
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "load": self.load.to_dict(),
            "steps": [{**s.to_dict(), "enabled": s.enabled} for s in self.steps],
        }
        if self.description:  # omit when blank so shipped template files stay uncluttered
            payload["description"] = self.description
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatchTemplate:
        name: Any = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise TemplateFormatError(
                "template is missing its required 'name' (a non-empty string)"
            )
        description: Any = data.get("description", "")
        if not isinstance(description, str):
            raise TemplateFormatError(
                f"'description' must be a string, got {type(description).__name__}"
            )
        load: Any = data.get("load", {})
        if not isinstance(load, dict):
            raise TemplateFormatError(f"'load' must be an object, got {type(load).__name__}")
        raw_steps: Any = data.get("steps", [])
        steps_type_name = type(raw_steps).__name__
        if isinstance(raw_steps, (str, dict)) or not isinstance(raw_steps, list):
            raise TemplateFormatError(f"'steps' must be a list, got {steps_type_name}")
        steps: list[BatchStep] = []
        for index, raw in enumerate(cast("list[Any]", raw_steps), start=1):
            if not isinstance(raw, dict):
                raise TemplateFormatError(
                    f"step {index} must be an object, got {type(raw).__name__}"
                )
            try:
                steps.append(_step_from_dict(cast("dict[str, Any]", raw)))
            except TemplateFormatError as exc:
                raise TemplateFormatError(f"step {index}: {exc}") from exc
        return cls(
            name=name.strip(),
            load=LoadConfig.from_dict(cast("dict[str, Any]", load)),
            steps=steps,
            description=description.strip(),
        )


def dump_templates(templates: list[BatchTemplate], path: Path) -> None:
    """Write templates to ``path`` as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [t.to_dict() for t in templates]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def load_templates(path: Path) -> list[BatchTemplate]:
    """Read templates from ``path`` (``[]`` if missing).

    Raises:
        TemplateFormatError: When the file is not valid JSON, is not a top-level array, or any
            template/step in it is malformed — with a message naming the template and step.
    """
    if not path.is_file():
        return []
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TemplateFormatError(f"{path.name}: not valid JSON ({exc})") from exc
    if not isinstance(raw, list):
        raise TemplateFormatError(
            f"{path.name}: the top level must be a JSON array of template objects "
            f"(got {type(raw).__name__}) — wrap a single template in [ ... ]"
        )
    templates: list[BatchTemplate] = []
    for index, item in enumerate(cast("list[Any]", raw), start=1):
        if not isinstance(item, dict):
            raise TemplateFormatError(
                f"{path.name}: template {index} must be an object, got {type(item).__name__}"
            )
        template_data = cast(dict[str, Any], item)
        try:
            templates.append(BatchTemplate.from_dict(template_data))
        except TemplateFormatError as exc:
            label = str(template_data.get("name", "")) or None
            where = f"template {index}" + (f" ({label!r})" if label else "")
            raise TemplateFormatError(f"{path.name}: {where}: {exc}") from exc
    return templates


# --------------------------------------------------------------------------------------
# Schema propagation + validation (drives schema-aware pickers and pre-run warnings)
# --------------------------------------------------------------------------------------
def _confidence_columns(step: NormalizeStep | ReferenceMatchStep | CompareStep) -> list[str]:
    """The optional score/review column names a confidence-enabled step adds."""
    names: list[str] = []
    if step.emit_score:
        names.append(step.resolved_score())
    if step.review_threshold > 0:
        names.append(step.resolved_review())
    return names


def columns_after(  # noqa: PLR0912 - one branch per step kind
    load: LoadConfig,
    steps: Sequence[BatchStep],
    upto: int,
    *,
    base: dict[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    """The columns present on each table after applying the first ``upto`` (enabled) steps.

    Starts from the loaded base schema (``load.columns`` projection or the full table schema) and
    folds each step's column effect — adds derived/canonical/type/match columns, applies
    filter/select projections, and creates aggregate output tables. Powers schema-aware column
    pickers and validation; disabled steps are ignored (they don't run).

    ``base`` overrides the assumed fresh-parse schema per table — pass a dataset input's actual
    columns (:func:`~uspto_assignments.tables.dataset_columns`) so a pipeline over a processed
    dataset validates against what the input really carries (e.g. pre-attached ``cpc_codes``).
    """
    cols: dict[str, list[str]] = {}
    for table in STORE_TABLES:
        chosen = load.columns.get(table)
        seed = (base or {}).get(table) or list(columns_for(table))
        cols[table] = list(chosen) if chosen else list(seed)

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
            if isinstance(step, NormalizeStep):
                for name in _confidence_columns(step):
                    add(step.table, name)
                if step.emit_type:
                    add(step.table, step.resolved_type())
        elif isinstance(step, CompareStep):
            if step.action == "flag":
                add(step.table, step.resolved_target())
            for name in _confidence_columns(step):
                add(step.table, name)
        elif isinstance(step, ReferenceMatchStep):
            add(step.table, step.resolved_target())
            add(step.table, step.resolved_matched())
            if step.id_column:
                add(step.table, step.resolved_id())
            for name in _confidence_columns(step):
                add(step.table, name)
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
        elif isinstance(step, FetchCpcStep | AttachCpcFileStep):
            for name in ("cpc_codes", "cpc_subclasses", "cpc_lookup_status"):
                add(step.table, name)
        elif isinstance(step, CpcMatchStep):
            cols[step.out_table] = list(PER_COLUMNS)
            cols[step.overall_table] = list(OVERALL_COLUMNS)
            if step.emit_class_matches:
                cols[step.class_match_table] = list(CLASS_MATCH_COLUMNS)
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
    if isinstance(step, FetchCpcStep | AttachCpcFileStep):
        return step.table, [step.column]
    if isinstance(step, CpcMatchStep):
        return step.table, [step.buyer_column, step.number_column, "cpc_codes"]
    return "", []  # ExportStep validated separately


class TemplateValidationError(ValueError):
    """Raised by ``run_batch(strict=True)`` when the template has validation warnings."""

    def __init__(self, warnings: list[str]) -> None:
        super().__init__(f"{len(warnings)} validation warning(s): " + "; ".join(warnings[:3]))
        self.warnings = warnings


def _reference_column_problems(step: ReferenceMatchStep) -> list[str]:
    """Warnings for configured reference columns missing from the (existing) reference file.

    Catches the classic mismatch — a compact reference built without an id column while the step
    still names ``assignee_id`` — before the run fails on an opaque pyarrow schema error.
    """
    try:
        file_columns = reference_columns(Path(step.reference_path), step.delimiter)
    except OSError:
        return []  # unreadable right now; the run itself will report it
    return [
        f"reference file has no column '{needed}' (has: {', '.join(file_columns)})."
        for needed in (step.name_column, step.id_column)
        if needed and needed not in file_columns
    ]


def validate_template(  # noqa: PLR0912 - one validation branch per step kind
    load: LoadConfig,
    steps: Sequence[BatchStep],
    *,
    base: dict[str, list[str]] | None = None,
) -> list[str]:
    """Return human-readable warnings about a template (missing columns/tables/reference files).

    ``base`` (optional) supplies the actual input schema per table — pass a dataset input's
    :func:`~uspto_assignments.tables.dataset_columns` so columns an earlier pipeline attached
    (``cpc_codes``, ``*_canonical``) don't warn as missing.
    """
    warnings: list[str] = []
    if not any(s.enabled for s in steps):
        warnings.append("The pipeline has no enabled steps.")
    warnings.extend(
        f"Load: table '{name}' selects no columns — it will be dropped entirely "
        f"(remove the entry to load all its columns)."
        for name, cols in load.columns.items()
        if not cols
    )
    for index, step in enumerate(steps, start=1):
        if not step.enabled:
            continue
        # columns available as input to this step
        available = columns_after(load, steps, index - 1, base=base)
        table, refs = _referenced_columns(step)
        if table and table not in available:
            warnings.append(f"Step {index} ({type(step).__name__}): table '{table}' is not loaded.")
        else:
            present = available.get(table, [])
            # cpc_match commonly runs on a processed dataset folder that already carries the
            # canonical/CPC columns from an earlier template (the documented 13→14 chain) —
            # static validation can't see a dataset input's schema, so soften that message.
            hint = (
                " (OK if the input is a processed dataset that already carries it)"
                if isinstance(step, CpcMatchStep)
                else ""
            )
            for column in refs:
                if column and column not in present:
                    warnings.append(
                        f"Step {index} ({type(step).__name__}): "
                        f"column '{column}' is not available on '{table}' yet.{hint}"
                    )
        if isinstance(step, FilterStep):
            warnings.extend(
                f"Step {index} (Filter): in_range on '{clause.column}' has no upper bound "
                f"(value2) — it will match nothing."
                for clause in step.clauses
                if clause.op == "in_range" and not clause.value2
            )
        if isinstance(step, ExportStep) and step.tables is not None:
            warnings.extend(
                f"Step {index} (Export): table '{name}' does not exist at this point — "
                f"it will be silently skipped."
                for name in step.tables
                if name not in available
            )
        if isinstance(step, ExportStep) and step.columns:
            for name, wanted in step.columns.items():
                present = available.get(name)
                if present is not None and wanted and not [c for c in wanted if c in present]:
                    warnings.append(
                        f"Step {index} (Export): none of the selected columns for '{name}' exist "
                        f"— the projection would be ignored and all columns exported."
                    )
        if isinstance(step, ReferenceMatchStep):
            if not step.reference_path:
                warnings.append(f"Step {index} (ReferenceMatch): no reference file set.")
            elif not Path(step.reference_path).is_file():
                warnings.append(
                    f"Step {index} (ReferenceMatch): reference file not found: "
                    f"{step.reference_path}"
                )
            else:  # the file exists — cheaply check it has the configured columns
                warnings.extend(
                    f"Step {index} (ReferenceMatch): {problem}"
                    for problem in _reference_column_problems(step)
                )
        if isinstance(step, AttachCpcFileStep):
            if not step.source_path:
                warnings.append(f"Step {index} (AttachCpcFile): no CPC file set.")
            elif not Path(step.source_path).is_file():
                warnings.append(
                    f"Step {index} (AttachCpcFile): CPC file not found: {step.source_path}"
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

    level: str  # "info" | "warning" | "error" | "success"
    message: str
    # "file_done" marks a per-file completion line — the UI drives its determinate progress bar
    # from this, not from the message text.
    kind: Literal["message", "file_done"] = "message"


@dataclass(slots=True)
class StepStat:
    """Per-step audit stats: how the working table changed when the step ran.

    Captured for every step of every file in a real run (``FileResult.steps``) and for
    previews. Fields stay picklable primitives — they cross the worker-process boundary.
    """

    index: int  # 1-based position in the template
    label: str  # human-readable step summary
    table: str
    rows_before: int
    rows_after: int
    columns_added: list[str] = field(default_factory=list[str])
    note: str = ""


@dataclass(slots=True)
class FileResult:
    """The outcome of processing one input file."""

    source: str
    ok: bool
    outputs: list[str] = field(default_factory=list[str])
    rows: dict[str, int] = field(default_factory=dict[str, int])
    error: str | None = None
    elapsed: float = 0.0  # wall-clock seconds spent on this file
    # (alias, canonical, score) pairs the normalize steps learned while processing this file.
    learned: list[tuple[str, str, int]] = field(default_factory=list[tuple[str, str, int]])
    steps: list[StepStat] = field(default_factory=list[StepStat])  # per-step audit trail
    step_outputs: list[str] = field(default_factory=list[str])  # per-step trace files (trace mode)


@dataclass(slots=True)
class BatchResult:
    """The aggregate outcome of a batch run."""

    succeeded: int
    failed: int
    results: list[FileResult]
    cancelled: bool = False  # True when the run stopped early via ``should_stop``
    warnings: list[str] = field(default_factory=list[str])  # pre-run validation warnings
    run_dir: str = ""  # the per-run output folder (holds manifest.json, run.log, sources)


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
            | AttachCpcFileStep
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
        score_target=f"{target}_score" if step.emit_score else "",
        review_target=f"{target}_review" if step.review_threshold > 0 else "",
        review_threshold=step.review_threshold,
        type_target=f"{target}_type" if step.emit_type else "",
        on_progress=on_progress,
    )


def _apply_classify(tables: dict[str, pa.Table], step: ClassifyStep, emit: OnEvent) -> None:
    table = tables.get(step.table)
    if table is None or step.column not in table.column_names:
        emit(BatchEvent("info", f"  skip classify: {step.table}.{step.column} not present"))
        return
    if step.method == "probablepeople" and not probablepeople_available():
        emit(
            BatchEvent(
                "warning",
                f"  probablepeople not installed — classify used rules for "
                f"{step.table}.{step.column} (the ML backend needs a Python 3.12 venv on Windows; "
                f"see requirements.txt)",
            )
        )
    if step.method == "model" and not model_available():  # only on a corrupt/partial install
        emit(
            BatchEvent(
                "warning",
                f"  built-in model artifact missing — classify used rules for "
                f"{step.table}.{step.column}",
            )
        )

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


def _compare_scores(table: pa.Table, step: CompareStep) -> Any:
    """Per-row similarity of ``left`` vs ``right`` as an int32 array (nulls propagate).

    Exact method: 100 where equal, 0 otherwise (vectorized). Fuzzy: the rapidfuzz score.
    """
    left: Any = pc.cast(table.column(step.left), pa.string())
    right: Any = pc.cast(table.column(step.right), pa.string())
    if step.method != "fuzzy":
        return pc.cast(pc.if_else(pc.equal(left, right), 100, 0), pa.int32())
    scorer_fn = get_scorer(step.scorer)
    return pa.array(
        [
            None if a is None or b is None else round(scorer_fn(a, b))
            for a, b in zip(left.to_pylist(), right.to_pylist(), strict=True)
        ],
        type=pa.int32(),
    )


def _replace_column(table: pa.Table, name: str, values: Any) -> pa.Table:
    if name in table.column_names:
        table = table.drop_columns([name])
    return table.append_column(name, values)


def _apply_compare(tables: dict[str, pa.Table], step: CompareStep, emit: OnEvent) -> None:
    table = tables.get(step.table)
    if table is None or step.left not in table.column_names or step.right not in table.column_names:
        emit(
            BatchEvent("info", f"  skip compare: {step.table}.{step.left}/{step.right} not present")
        )
        return
    scores: Any = _compare_scores(table, step)
    if step.method != "fuzzy":  # exact: equality decides, regardless of threshold
        mask: Any = pc.fill_null(pc.equal(scores, 100), False)
    else:
        mask = pc.fill_null(pc.greater_equal(scores, step.threshold), False)

    # Two empty values are NOT a match: comparing e.g. two ``*_assignee_id`` columns where
    # neither side resolved ("" == "") must not count the rows as the same entity — with
    # ``drop_matches`` that would silently drop every doubly-unmatched row.
    source_table = table  # non-None binding for the closure (narrowing doesn't cross scopes)

    def _non_empty(column: str) -> Any:
        col: Any = pc.cast(source_table.column(column), pa.string())
        return pc.fill_null(pc.greater(pc.utf8_length(col), 0), False)

    mask = pc.and_(mask, pc.and_(_non_empty(step.left), _non_empty(step.right)))
    # Confidence columns are appended BEFORE any filtering so kept rows carry their score.
    if step.emit_score:
        table = _replace_column(table, step.resolved_score(), scores)
    if step.review_threshold > 0:
        cap = min(step.review_threshold, 100)  # exact matches (100) never flag
        in_review: Any = pc.and_(mask, pc.less(pc.fill_null(scores, 0), cap))
        review: Any = pc.if_else(in_review, pa.scalar("true"), pa.scalar("false"))
        table = _replace_column(table, step.resolved_review(), review)
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
        tables[step.table] = _replace_column(table, target, flags)
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
        score_col=step.resolved_score() if step.emit_score else "",
        review_col=step.resolved_review() if step.review_threshold > 0 else "",
        review_threshold=step.review_threshold,
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


def _check_filter_columns(table: pa.Table, step: FilterStep) -> None:
    """Raise a clear error when a filter clause (or sort) names a column the table lacks.

    Without this, pyarrow surfaces an opaque ``KeyError: 'Field "x" does not exist in schema'`` and
    the whole file aborts with no output — commonly because an earlier step renamed/dropped the
    column, the wrong table was chosen, or a clause was added with no column selected (empty name).
    """
    available = set(table.column_names)
    needed = [c.column for c in step.clauses]
    if step.sort and step.sort[0]:
        needed.append(step.sort[0])
    missing = [c for c in dict.fromkeys(needed) if c not in available]
    if missing:
        shown = ", ".join(repr(c) for c in missing)
        raise ValueError(
            f"filter on '{step.table}': column(s) {shown} not present "
            f"(available: {', '.join(table.column_names)}). An earlier step may have renamed or "
            "dropped the column, or a filter row was added without choosing a column — fix the "
            "clause to name an existing column."
        )


def _apply_filter(tables: dict[str, pa.Table], step: FilterStep, emit: OnEvent) -> None:
    table = tables.get(step.table)
    if table is None:
        emit(BatchEvent("info", f"  skip filter: table '{step.table}' not present"))
        return
    _check_filter_columns(table, step)
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
    tables: dict[str, pa.Table],
    step: ExportStep,
    source_dir: Path,
    emit: OnEvent,
    export_prefix: str = "",
) -> list[str]:
    # Default export order: the known store tables first, then any derived/aggregate tables.
    # ``tables=[]`` means "nothing" (only reachable via hand-edited templates); ``None`` means all.
    extras = [n for n in tables if n not in STORE_TABLES]
    names = (
        step.tables
        if step.tables is not None
        else [n for n in STORE_TABLES if n in tables] + extras
    )
    if not names:
        emit(BatchEvent("info", "  export: no tables selected — nothing written"))
    written: list[str] = []
    for name in names:
        table = tables.get(name)
        if table is None:
            emit(
                BatchEvent(
                    "warning",
                    f"  export: table '{name}' does not exist at this point — skipped "
                    f"(check the name against the tables earlier steps create)",
                )
            )
            continue
        chosen = (step.columns or {}).get(name)
        if chosen and not [c for c in chosen if c in table.column_names]:
            emit(
                BatchEvent(
                    "warning",
                    f"  export: none of the selected columns for '{name}' exist — "
                    f"exporting all columns instead",
                )
            )
        table = _project_for_export(table, step, name)
        # Flat "convert" mode (export_prefix set) names files by source (<stem>_<table>.<ext>) in a
        # shared folder and overwrites on re-run; normal mode keeps <table>.<ext> non-clobbering.
        dest = source_dir / f"{export_prefix}{name}{FORMAT_SUFFIX[step.fmt]}"
        path = dest if export_prefix else unique_path(dest)
        export(table, path, step.fmt)
        written.append(str(path))
        emit(BatchEvent("info", f"  export {name} → {path.name} ({table.num_rows:,} rows)"))
    return written


def inputs_schema_base(inputs: Sequence[Path]) -> dict[str, list[str]] | None:
    """The union of dataset-folder inputs' actual schemas, or None when none are datasets.

    Validation otherwise assumes the fresh-parse schema and falsely warns that columns a prior
    pipeline attached (``cpc_codes``, ``*_canonical`` — the documented 13→14 chain) "are not
    available yet". Raw XML/ZIP inputs contribute nothing here: for them the static schema is
    exact, and a genuinely missing column still fails visibly at run time.
    """
    base: dict[str, list[str]] | None = None
    for source in inputs:
        if not source.is_dir():
            continue
        for table, names in dataset_columns(source).items():
            base = base if base is not None else {}
            merged = base.setdefault(table, [])
            merged.extend(n for n in names if n not in merged)
    return base


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
    _warn_low_cpc_hit_rate(stats, ctx.config.match.hit_rate_floor, "fetch_cpc", emit)
    if stats.uncached_offline:
        emit(
            BatchEvent(
                "info",
                f"  note: {stats.uncached_offline:,} grant patents are uncached — enable network "
                f"for this run to fetch their CPC codes",
            )
        )


def _warn_low_cpc_hit_rate(stats: Any, floor: float, step_name: str, emit: OnEvent) -> None:
    """Escalate a suspicious CPC join to a warning: looked-up rows that mostly failed to resolve.

    ``cpc_match`` aborts below the floor; the attach steps must not fail the run (partial CPC is
    still useful) but a near-zero hit-rate almost always means the patent-number key formats are
    misaligned — surfacing it as info only lets a broken join look like success.
    """
    if stats.looked_up and stats.hit_rate < floor:
        emit(
            BatchEvent(
                "warning",
                f"  {step_name}: CPC hit-rate {stats.hit_rate:.0%} is below {floor:.0%} — the "
                f"patent numbers and the CPC source's key format are likely misaligned "
                f"(expected bare grant numbers like 10987654)",
            )
        )


def _apply_attach_cpc_file(
    tables: dict[str, pa.Table], step: AttachCpcFileStep, ctx: CpcRunContext, emit: OnEvent
) -> None:
    table = tables.get(step.table)
    if table is None:
        emit(BatchEvent("info", f"  skip attach_cpc_file: table '{step.table}' not present"))
        return
    if step.column not in table.column_names:
        emit(
            BatchEvent(
                "info", f"  skip attach_cpc_file: column '{step.column}' not in '{step.table}'"
            )
        )
        return
    if not step.source_path or not Path(step.source_path).is_file():
        emit(BatchEvent("info", f"  skip attach_cpc_file: CPC file '{step.source_path}' missing"))
        return
    out, stats = attach_cpc_from_file(
        table,
        number_column=step.column,
        kind_column=step.kind_column,
        source_path=Path(step.source_path),
        patent_column=step.patent_column,
        code_column=step.code_column,
        separator=step.separator,
    )
    tables[step.table] = out
    emit(
        BatchEvent(
            "info",
            f"  attach_cpc_file {step.table}.{step.column}: {stats.found:,}/{stats.eligible:,} "
            f"grants matched from {Path(step.source_path).name} (hit-rate {stats.hit_rate:.0%})",
        )
    )
    _warn_low_cpc_hit_rate(stats, ctx.config.match.hit_rate_floor, "attach_cpc_file", emit)


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
    per, overall, class_table, report = match_portfolio(
        table,
        footprints,
        config=match_cfg,
        buyer_column=step.buyer_column,
        number_column=step.number_column,
        kind_column=step.kind_column,
        date_column=step.date_column,
        emit_class_matches=step.emit_class_matches,
    )
    tables[step.out_table] = per
    tables[step.overall_table] = overall
    class_note = ""
    if step.emit_class_matches:
        tables[step.class_match_table] = class_table
        class_note = f"; {class_table.num_rows:,} class match(es) → {step.class_match_table}"
    emit(
        BatchEvent(
            "info",
            f"  cpc_match: {report.portfolio_patents} portfolio patent(s), buyer hit-rate "
            f"{report.buyer_stats.hit_rate:.0%}; {report.matched_pairs} matched pair(s) → "
            f"{per.num_rows:,} ranked rows across {report.buyers_out} buyer(s){class_note}",
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
    export_prefix: str = "",
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
    elif isinstance(step, AttachCpcFileStep):
        _apply_attach_cpc_file(tables, step, _resolve_cpc_ctx(cpc_ctx), emit)
    elif isinstance(step, CpcMatchStep):
        _apply_cpc_match(tables, step, _resolve_cpc_ctx(cpc_ctx), emit)
    else:
        return _apply_export(tables, step, source_dir, emit, export_prefix)
    return []


_LARGE_DROP_FRACTION = 0.99  # warn (info) when a step removes at least this share of a table's rows


def _warn_if_emptied(  # noqa: PLR0913 - a small guard threading the loop's locals
    tables: dict[str, pa.Table],
    step: BatchStep,
    index: int,
    table_name: str,
    before: int | None,
    emit: OnEvent,
) -> str:
    """Surface the #1 silent failure: a step that drops a non-empty table to 0 (or nearly).

    Returns the note recorded in the step audit trail ("" when nothing noteworthy).
    """
    if not before or not table_name:
        return ""
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
        return "⚠ dropped all rows"
    if after <= before * (1 - _LARGE_DROP_FRACTION):
        emit(
            BatchEvent(
                "info",
                f"  note: step {index} ({kind}) dropped {before - after:,} of {before:,} rows "
                f"from '{table_name}'",
            )
        )
        return f"dropped {before - after:,} of {before:,} rows"
    return ""


def _trace_step_outputs(
    tables: dict[str, pa.Table], touched: list[str], index: int, source_dir: Path
) -> list[str]:
    """Write a step's resulting table(s) to ``<source>/steps/NN_<table>.parquet`` for review."""
    steps_dir = source_dir / "steps"
    steps_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name in touched:
        table = tables.get(name)
        if table is None:
            continue
        path = steps_dir / f"{index:02d}_{_safe_name(name)}.parquet"
        export(table, path, "parquet")
        written.append(str(path))
    return written


def _process_input(  # noqa: PLR0913 - threads collaborators; one branch per kind
    template: BatchTemplate,
    source: Path,
    template_dir: Path,
    source_dir: Path,
    emit: OnEvent,
    on_parse: OnParse | None = None,
    memory: EntityMemory | None = None,
    cpc_ctx: CpcRunContext | None = None,
    trace_steps: bool = False,
    export_prefix: str = "",
) -> FileResult:
    memory = memory if memory is not None else EntityMemory()
    learned_before = len(memory.learned)
    start = time.monotonic()
    work_dir: Path | None = None
    step_stats: list[StepStat] = []  # audit trail (partial stats survive a mid-run failure)
    step_outputs: list[str] = []  # per-step trace files, when trace_steps is on
    try:
        if source.is_file():
            work_dir = Path(tempfile.mkdtemp(prefix="uspto_batch_"))
        tables = _load_tables(template, source, work_dir or template_dir, on_parse)
        source_dir.mkdir(parents=True, exist_ok=True)
        outputs: list[str] = []
        used_targets: set[tuple[str, str]] = set()  # (table, target) claimed (collision guard)
        for index, step in enumerate(template.steps, start=1):
            table_name: str = getattr(step, "table", "")
            if not step.enabled:  # UI-disabled step: skip without deleting
                step_stats.append(
                    StepStat(index, describe_step(step), table_name, 0, 0, note="disabled")
                )
                continue
            before = tables[table_name].num_rows if table_name in tables else None
            before_cols: set[str] = (
                set(tables[table_name].column_names) if table_name in tables else set()
            )
            before_tables = set(tables)
            outputs.extend(
                _apply_step(
                    tables, step, memory, used_targets, source_dir, emit, cpc_ctx, export_prefix
                )
            )
            note = _warn_if_emptied(tables, step, index, table_name, before, emit)
            after_table = tables.get(table_name)
            step_stats.append(
                StepStat(
                    index,
                    describe_step(step),
                    table_name,
                    rows_before=before or 0,
                    rows_after=after_table.num_rows if after_table is not None else 0,
                    columns_added=(
                        sorted(set(after_table.column_names) - before_cols)
                        if after_table is not None
                        else []
                    ),
                    note=note,
                )
            )
            if trace_steps:  # the mutated table plus any new tables the step created
                touched = ([table_name] if table_name in tables else []) + sorted(
                    set(tables) - before_tables
                )
                step_outputs.extend(_trace_step_outputs(tables, touched, index, source_dir))
        rows = {name: table.num_rows for name, table in tables.items()}
        result = FileResult(
            str(source),
            ok=True,
            outputs=outputs,
            rows=rows,
            learned=memory.learned[learned_before:],
            steps=step_stats,
            step_outputs=step_outputs,
        )
    except Exception as exc:  # per-file isolation: log, record, and keep going
        logger.exception("batch: failed on %s", source)
        result = FileResult(
            str(source),
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            steps=step_stats,
            step_outputs=step_outputs,
        )
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
    trace_steps: bool = False,
    export_prefix: str = "",
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
        trace_steps=trace_steps,
        export_prefix=export_prefix,
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


def _assign_export_prefixes(inputs: Sequence[Path]) -> list[str]:
    """One distinct filename prefix per input (flat convert mode), so files are named by source.

    Mirrors :func:`_assign_source_dirs`: same-stem inputs from different folders would collide on
    ``<stem>_<table>.parquet`` in the shared flat folder, so each gets a serial ``<stem> (n)_``
    prefix decided up front (parallel-safe — every worker targets a distinct filename).
    """
    claimed: set[str] = set()
    prefixes: list[str] = []
    for source in inputs:
        stem = source.stem
        candidate = stem
        counter = 1
        while candidate in claimed:
            candidate = f"{stem} ({counter})"
            counter += 1
        claimed.add(candidate)
        prefixes.append(f"{candidate}_")
    return prefixes


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


def _run_parallel(  # noqa: PLR0913, PLR0915 - internal helper threading the run's collaborators
    template: BatchTemplate,
    inputs: list[Path],
    source_dirs: Sequence[Path],
    template_dir: Path,
    workers: int,
    emit: OnEvent,
    memory: EntityMemory,
    cpc_ctx: CpcRunContext | None = None,
    should_stop: Callable[[], bool] = _never_stop,
    trace_steps: bool = False,
    export_prefixes: Sequence[str] | None = None,
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
    prefixes: Sequence[str] = export_prefixes if export_prefixes is not None else [""] * len(inputs)

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
                    _process_one,
                    template,
                    src,
                    template_dir,
                    src_dir,
                    event_queue,
                    memory,
                    cpc_ctx,
                    trace_steps,
                    prefix,
                ): src
                for src, src_dir, prefix in zip(inputs, source_dirs, prefixes, strict=True)
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


def _write_run_log(run_dir: Path, template: BatchTemplate, results: list[FileResult]) -> None:
    log_path = run_dir / "run.log"
    lines = [f"Batch template: {template.name}", f"Run folder: {run_dir.name}", ""]
    for result in results:
        status = "OK" if result.ok else f"FAILED: {result.error}"
        lines.append(
            f"{result.source}: {status}  {result.elapsed:.1f}s  "
            f"rows={result.rows} outputs={len(result.outputs)}"
        )
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_relative(raw: str, run_dir: Path) -> str:
    """A path relative to the run folder (defensive: absolute if somehow outside it)."""
    path = Path(raw)
    try:
        return str(path.relative_to(run_dir))
    except ValueError:
        return str(path)


def _output_entries(result: FileResult, run_dir: Path) -> list[dict[str, Any]]:
    """Manifest/summary rows for one file's written outputs (paths relative to the run folder)."""
    return [
        {
            "path": _run_relative(raw, run_dir),
            "table": Path(raw).stem,
            "format": Path(raw).suffix.lstrip("."),
            "rows": result.rows.get(Path(raw).stem),
        }
        for raw in result.outputs
    ]


def _write_manifest(  # noqa: PLR0913 - snapshotting the whole run takes the run's collaborators
    run_dir: Path,
    template: BatchTemplate,
    warnings: list[str],
    results: list[FileResult],
    *,
    timestamp: str,
    workers: int,
    cancelled: bool,
    strict: bool,
    elapsed: float,
    inputs: list[Path],
) -> None:
    """Write ``manifest.json``: the full audit record of one batch run."""
    payload: dict[str, Any] = {
        "schema": 1,
        "template": {
            "name": template.name,
            "steps": [
                {
                    "index": index,
                    "kind": type(step).__name__,
                    "enabled": step.enabled,
                    "summary": describe_step(step),
                }
                for index, step in enumerate(template.steps, start=1)
            ],
        },
        "timestamp": timestamp,
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "duration_seconds": round(elapsed, 1),
        "workers": workers,
        "cancelled": cancelled,
        "strict": strict,
        "warnings": warnings,
        "inputs": [str(path) for path in inputs],
        "summary": {
            "succeeded": sum(1 for r in results if r.ok),
            "failed": sum(1 for r in results if not r.ok),
        },
        "files": [
            {
                "source": result.source,
                "ok": result.ok,
                "error": result.error,
                "elapsed": round(result.elapsed, 1),
                "rows": result.rows,
                "outputs": _output_entries(result, run_dir),
                "steps": [asdict(stat) for stat in result.steps],
                "step_outputs": [_run_relative(p, run_dir) for p in result.step_outputs],
            }
            for result in results
        ],
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _write_summary_xlsx(  # noqa: PLR0913 - snapshotting the whole run takes the run's collaborators
    run_dir: Path,
    template: BatchTemplate,
    warnings: list[str],
    results: list[FileResult],
    *,
    timestamp: str,
    workers: int,
    cancelled: bool,
    elapsed: float,
    inputs: list[Path],
) -> None:
    """Write ``summary.xlsx``: the manifest's audit record as a human-readable workbook."""
    run_rows = [
        ("template", template.name),
        ("timestamp", timestamp),
        ("duration_seconds", f"{elapsed:.1f}"),
        ("workers", str(workers)),
        ("cancelled", str(cancelled).lower()),
        ("succeeded", str(sum(1 for r in results if r.ok))),
        ("failed", str(sum(1 for r in results if not r.ok))),
        *((f"input {i}", str(path)) for i, path in enumerate(inputs, start=1)),
        *((f"warning {i}", w) for i, w in enumerate(warnings, start=1)),
    ]
    run_sheet = pa.table(
        {
            "key": pa.array([k for k, _ in run_rows], type=pa.string()),
            "value": pa.array([v for _, v in run_rows], type=pa.string()),
        }
    )

    step_cols: dict[str, list[Any]] = {
        "file": [],
        "step": [],
        "description": [],
        "rows_before": [],
        "rows_after": [],
        "delta": [],
        "note": [],
    }
    for result in results:
        name = Path(result.source).name
        for stat in result.steps:
            step_cols["file"].append(name)
            step_cols["step"].append(stat.index)
            step_cols["description"].append(stat.label)
            step_cols["rows_before"].append(stat.rows_before)
            step_cols["rows_after"].append(stat.rows_after)
            step_cols["delta"].append(stat.rows_after - stat.rows_before)
            step_cols["note"].append(stat.note)
    steps_sheet = (
        pa.table(step_cols)
        if step_cols["file"]
        else pa.table({name: pa.array([], type=pa.string()) for name in step_cols})
    )

    out_cols: dict[str, list[Any]] = {"file": [], "path": [], "table": [], "rows": [], "format": []}
    for result in results:
        name = Path(result.source).name
        for entry in _output_entries(result, run_dir):
            out_cols["file"].append(name)
            out_cols["path"].append(entry["path"])
            out_cols["table"].append(entry["table"])
            out_cols["rows"].append(entry["rows"])
            out_cols["format"].append(entry["format"])
    outputs_sheet = (
        pa.table(out_cols)
        if out_cols["file"]
        else pa.table({name: pa.array([], type=pa.string()) for name in out_cols})
    )

    write_workbook(
        run_dir / "summary.xlsx",
        {"run": run_sheet, "steps": steps_sheet, "outputs": outputs_sheet},
    )


_RUNS_INDEX_HEADER = [
    "timestamp",
    "template",
    "inputs",
    "succeeded",
    "failed",
    "cancelled",
    "run_dir",
]


def _append_runs_index(  # noqa: PLR0913 - one ledger line per run
    out_root: Path,
    *,
    timestamp: str,
    template_name: str,
    inputs: int,
    succeeded: int,
    failed: int,
    cancelled: bool,
    run_dir: Path,
) -> None:
    """Append one line per run to ``<out_root>/runs_index.csv`` (the cross-run ledger).

    Single-line appends only (no read-modify-write), so concurrent runs into the same
    ``out_root`` interleave rows but never corrupt each other.
    """
    index_path = out_root / "runs_index.csv"
    new_file = not index_path.exists()
    with index_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if new_file:
            writer.writerow(_RUNS_INDEX_HEADER)
        writer.writerow(
            [
                timestamp,
                template_name,
                inputs,
                succeeded,
                failed,
                str(cancelled).lower(),
                str(run_dir.relative_to(out_root)),
            ]
        )


def run_batch(  # noqa: PLR0913, PLR0912, PLR0915 - clear public entry point, keyword-only options
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
    strict: bool = False,
    trace_steps: bool = False,
    flat_output: bool = False,
) -> BatchResult:
    """Run ``template`` over ``inputs``, writing outputs under ``out_root``.

    Args:
        template: The pipeline to apply to each input.
        inputs: Files (``.xml``/``.zip``) and/or dataset folders to process.
        out_root: Root output directory. Each run gets its own self-contained folder:
            ``<out_root>/<template-name>/run_<timestamp>/`` holding ``manifest.json``,
            ``run.log``, and one subfolder per source. In ``flat_output`` mode this structure is
            bypassed (see below).
        workers: Number of parallel worker processes (``1`` = sequential with live per-step events).
        timestamp: Stamp naming the run folder (defaults to the current local time).
        memory: Entity memory for any normalize steps; new aliases learned during the run are
            merged back into it (persist it afterwards to keep learning). A fresh one is used if
            ``None``.
        on_event: Optional callback receiving :class:`BatchEvent`s as processing proceeds.
        should_stop: Optional cancellation probe, polled between files (cancellation is per-file:
            the file in flight finishes, remaining inputs are skipped). Must be thread-safe.
        strict: When True, any ``validate_template`` warning aborts the run (before any output
            is written) by raising :class:`TemplateValidationError`. The default emits the
            warnings as error events and continues.
        trace_steps: When True, each enabled step's resulting table(s) are written to
            ``<source-stem>/steps/NN_<table>.parquet`` for manual review of every intermediate.
        flat_output: "Convert" mode. When True, outputs land **directly in ``out_root``** named by
            source (``<source-stem>_<table>.<ext>``) instead of per-source subfolders under a
            timestamped run folder, and the manifest / summary / runs-index / run-log audit
            artifacts are skipped. Re-runs overwrite same-named files. ``trace_steps`` is ignored.

    Returns:
        A :class:`BatchResult` summarizing successes, failures, and per-file details;
        ``result.cancelled`` is True when the run stopped early.
    """
    emit = on_event or _noop_event
    stop = should_stop or _never_stop
    validation_warnings = validate_template(
        template.load, template.steps, base=inputs_schema_base(inputs)
    )
    for warning in validation_warnings:
        emit(BatchEvent("error", f"⚠ {warning}"))
    if strict and validation_warnings:
        raise TemplateValidationError(validation_warnings)
    memory = memory if memory is not None else EntityMemory()
    run_start = time.monotonic()
    timestamp = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    if flat_output:
        if trace_steps:
            emit(BatchEvent("info", "Convert mode: step-output tracing is ignored (no run folder)"))
        out_root.mkdir(parents=True, exist_ok=True)
        run_dir = out_root  # everything lands directly here, no template/run subfolders
        source_dirs = [out_root] * len(inputs)
        export_prefixes = _assign_export_prefixes(inputs)  # <source-stem>_ per file, deduped
        trace_steps = False
    else:
        template_dir = out_root / _safe_name(template.name)
        run_dir = unique_path(template_dir / f"run_{timestamp}")  # same stamp twice -> " (1)"
        run_dir.mkdir(parents=True, exist_ok=True)
        source_dirs = _assign_source_dirs(inputs, run_dir)
        export_prefixes = [""] * len(inputs)
    results: list[FileResult] = []
    cancelled = False

    if workers > 1 and len(inputs) > 1:
        emit(BatchEvent("info", f"Processing {len(inputs)} inputs with {workers} workers…"))
        results, cancelled = _run_parallel(
            template,
            inputs,
            source_dirs,
            run_dir,
            workers,
            emit,
            memory,
            cpc_ctx,
            should_stop=stop,
            trace_steps=trace_steps,
            export_prefixes=export_prefixes,
        )
        # Workers mutate pickled copies of the memory; merge what they learned back in. (The
        # sequential path shares ``memory`` in-process, so its learned pairs are already there.)
        for result in results:
            memory.apply_learned(result.learned)
    else:
        for source, source_dir, export_prefix in zip(
            inputs, source_dirs, export_prefixes, strict=True
        ):
            if stop():
                cancelled = True
                emit(BatchEvent("info", "Batch cancelled — skipping remaining inputs"))
                break
            emit(BatchEvent("info", f"▶ {source.name}"))
            result = _process_input(
                template,
                source,
                run_dir,
                source_dir,
                emit,
                on_parse=lambda count: emit(
                    BatchEvent("info", f"  parsing… {count:,} assignments")
                ),
                memory=memory,
                cpc_ctx=cpc_ctx,
                trace_steps=trace_steps,
                export_prefix=export_prefix,
            )
            _emit_file_done(result, emit)
            results.append(result)

    if not flat_output:
        _write_run_log(run_dir, template, results)
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
    if not flat_output:  # "convert" mode intentionally writes only the parquet files
        _write_manifest(
            run_dir,
            template,
            validation_warnings,
            results,
            timestamp=timestamp,
            workers=workers,
            cancelled=cancelled,
            strict=strict,
            elapsed=elapsed,
            inputs=inputs,
        )
        _write_summary_xlsx(
            run_dir,
            template,
            validation_warnings,
            results,
            timestamp=timestamp,
            workers=workers,
            cancelled=cancelled,
            elapsed=elapsed,
            inputs=inputs,
        )
        _append_runs_index(
            out_root,
            timestamp=timestamp,
            template_name=template.name,
            inputs=len(inputs),
            succeeded=succeeded,
            failed=failed,
            cancelled=cancelled,
            run_dir=run_dir,
        )
    return BatchResult(
        succeeded=succeeded,
        failed=failed,
        results=results,
        cancelled=cancelled,
        warnings=validation_warnings,
        run_dir=str(run_dir),
    )


# --------------------------------------------------------------------------------------
# Preview (dry-run on a small sample)
# --------------------------------------------------------------------------------------
PREVIEW_LIMIT = 1000


def _confidence_suffix(step: NormalizeStep | ReferenceMatchStep | CompareStep) -> str:
    """The steps-list marker for confidence options (e.g. " · score · review<95")."""
    parts = ""
    if step.emit_score:
        parts += " · score"
    if step.review_threshold > 0:
        parts += f" · review<{step.review_threshold}"
    return parts


def describe_step(step: BatchStep) -> str:  # noqa: PLR0911, PLR0912 - one line per step kind
    """A one-line human summary of ``step`` (used by the UI steps list, docs, and manifests)."""
    if isinstance(step, FilterStep):
        clause_count = len(step.clauses)
        return f"Filter · {step.table} · {clause_count} clause(s) · {step.combine.upper()}"
    if isinstance(step, NormalizeStep):
        split = f" · split '{step.separator}'" if step.separator else ""
        learn = "" if step.learn else " · match-only"
        type_marker = " · type" if step.emit_type else ""
        return (
            f"Normalize · {step.table}.{step.column} → {step.resolved_target()} "
            f"(≥{step.threshold}){split}{learn}{_confidence_suffix(step)}{type_marker}"
        )
    if isinstance(step, DedupeStep):
        key = ", ".join(step.subset) if step.subset else "whole row"
        return f"Deduplicate · {step.table} · key: {key}"
    if isinstance(step, SelectStep):
        return f"Select · {step.table} · keep {len(step.columns)} column(s)"
    if isinstance(step, SortStep):
        return f"Sort · {step.table} by {step.column} · {'asc' if step.ascending else 'desc'}"
    if isinstance(step, DeriveStep):
        return f"Derive · {step.table}.{step.resolved_target()} = {step.op}({step.source})"
    if isinstance(step, AggregateStep):
        return f"Aggregate · {step.table} by {', '.join(step.group_by)} → {step.resolved_out()}"
    if isinstance(step, ClassifyStep):
        return f"Classify · {step.table}.{step.column} → {step.resolved_target()} ({step.method})"
    if isinstance(step, CompareStep):
        return (
            f"Compare · {step.table} · {step.left} vs {step.right} · {step.method} · "
            f"{step.action}{_confidence_suffix(step)}"
        )
    if isinstance(step, TransferTypeStep):
        return f"Transfer type · {step.table} · {step.assignor_type} → {step.assignee_type}"
    if isinstance(step, ReferenceMatchStep):
        ref = Path(step.reference_path).name or "(no file)"
        return (
            f"Reference match · {step.table}.{step.column} vs {ref} · "
            f"{step.action}{_confidence_suffix(step)}"
        )
    if isinstance(step, FetchCpcStep):
        return f"Fetch CPC · {step.table}.{step.column} → cpc_codes"
    if isinstance(step, AttachCpcFileStep):
        src = Path(step.source_path).name or "(no file)"
        return f"Attach CPC file · {step.table}.{step.column} vs {src} → cpc_codes"
    if isinstance(step, CpcMatchStep):
        portfolio = Path(step.portfolio_path).name or "(no file)"
        classes = " + class matches" if step.emit_class_matches else ""
        return (
            f"CPC match · {step.table} vs {portfolio} · {step.portfolio_mode} → "
            f"{step.out_table}{classes}"
        )
    tables = "all tables" if step.tables is None else ", ".join(step.tables)
    return f"Export · {step.fmt} · {tables}"


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
    label_of = describe if describe is not None else describe_step
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
