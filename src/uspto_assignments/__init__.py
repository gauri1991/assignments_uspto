"""USPTO patent-assignment toolkit — parse assignment XML/ZIP into normalized tables.

Public API (import from the package root):

    from uspto_assignments import iter_records, parse_to_store, open_store
    from uspto_assignments import filters, exporters   # filter/sort + multi-format export

The package is intentionally UI-agnostic: the desktop app in ``uspto_assignments_ui`` imports
from here, never the reverse.
"""

from __future__ import annotations

from . import batch, exporters, filters, query
from .batch import (
    LEGACY_NORMALIZE_TARGET,
    AggregateStep,
    BatchEvent,
    BatchResult,
    BatchStep,
    BatchTemplate,
    ClassifyStep,
    CompareStep,
    CpcMatchStep,
    DedupeStep,
    DeriveStep,
    ExportStep,
    FetchCpcStep,
    FileResult,
    FilterStep,
    LoadConfig,
    NormalizeStep,
    ReferenceMatchStep,
    SelectStep,
    SortStep,
    StepStat,
    TransferTypeStep,
    columns_after,
    describe_step,
    dump_templates,
    load_templates,
    run_batch,
    run_preview,
    validate_template,
)
from .classify import classify_column, classify_name, classify_value
from .conveyance import classify_conveyance, conveyance_type_column
from .cpcconfig import (
    CpcCacheConfig,
    CpcConfig,
    CpcMatchConfig,
    CpcSourceConfig,
    load_config,
    save_config,
)
from .cpcmatch import attach_cpc, load_portfolio_footprint, match_portfolio
from .datasource import CpcCache, CpcRunContext, make_source
from .dictionary import ResolutionDictionary, build_dictionary, load_dictionary
from .exporters import FORMAT_SUFFIX, ExportFormat, export, export_store
from .filters import CombineMode, FilterClause
from .ledger import build_ledger, reconcile_cpc, top_buyers
from .model import (
    DEFAULT_BATCH_SIZE,
    EXCEL_MAX_ROWS,
    FLAT_COLUMNS,
    TABLE_TYPES,
    Assignee,
    Assignment,
    Assignor,
    ExtractedRecord,
    PropertyRow,
    columns_for,
    flat_schema,
    schema_for,
)
from .naming import scope_suffix, unique_path
from .normalize import DEFAULT_SCORER, EntityMemory, normalize_column, scorer_names
from .parser import extract, iter_assignments, iter_records
from .propid import add_doc_columns, doc_type_for, normalize_patent_id
from .query import Query, dump_queries, load_queries
from .reference import (
    ReferenceGazetteer,
    build_reference,
    extract_distinct_reference,
    load_reference,
    match_column,
    reference_columns,
)
from .resolution import CappedBlockIndex, ResolvedMention, resolve_mentions
from .tables import (
    STORE_TABLES,
    TableStore,
    flat_rows,
    open_dataset,
    open_parquet_store,
    open_store,
    parse_to_store,
    rows_to_table,
)
from .writers import write_excel, write_parquet

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_SCORER",
    "EXCEL_MAX_ROWS",
    "FLAT_COLUMNS",
    "FORMAT_SUFFIX",
    "LEGACY_NORMALIZE_TARGET",
    "STORE_TABLES",
    "TABLE_TYPES",
    "AggregateStep",
    "Assignee",
    "Assignment",
    "Assignor",
    "BatchEvent",
    "BatchResult",
    "BatchStep",
    "BatchTemplate",
    "CappedBlockIndex",
    "ClassifyStep",
    "CombineMode",
    "CompareStep",
    "CpcCache",
    "CpcCacheConfig",
    "CpcConfig",
    "CpcMatchConfig",
    "CpcMatchStep",
    "CpcRunContext",
    "CpcSourceConfig",
    "DedupeStep",
    "DeriveStep",
    "EntityMemory",
    "ExportFormat",
    "ExportStep",
    "ExtractedRecord",
    "FetchCpcStep",
    "FileResult",
    "FilterClause",
    "FilterStep",
    "LoadConfig",
    "NormalizeStep",
    "PropertyRow",
    "Query",
    "ReferenceGazetteer",
    "ReferenceMatchStep",
    "ResolutionDictionary",
    "ResolvedMention",
    "SelectStep",
    "SortStep",
    "StepStat",
    "TableStore",
    "TransferTypeStep",
    "add_doc_columns",
    "attach_cpc",
    "batch",
    "build_dictionary",
    "build_ledger",
    "build_reference",
    "classify_column",
    "classify_conveyance",
    "classify_name",
    "classify_value",
    "columns_after",
    "columns_for",
    "conveyance_type_column",
    "describe_step",
    "doc_type_for",
    "dump_queries",
    "dump_templates",
    "export",
    "export_store",
    "exporters",
    "extract",
    "extract_distinct_reference",
    "filters",
    "flat_rows",
    "flat_schema",
    "iter_assignments",
    "iter_records",
    "load_config",
    "load_dictionary",
    "load_portfolio_footprint",
    "load_queries",
    "load_reference",
    "load_templates",
    "make_source",
    "match_column",
    "match_portfolio",
    "normalize_column",
    "normalize_patent_id",
    "open_dataset",
    "open_parquet_store",
    "open_store",
    "parse_to_store",
    "query",
    "reconcile_cpc",
    "reference_columns",
    "resolve_mentions",
    "rows_to_table",
    "run_batch",
    "run_preview",
    "save_config",
    "schema_for",
    "scope_suffix",
    "scorer_names",
    "top_buyers",
    "unique_path",
    "validate_template",
    "write_excel",
    "write_parquet",
]
