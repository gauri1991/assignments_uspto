"""Turn extracted records into columnar tables.

This module holds the record→table transforms shared by every output path (Parquet, Excel,
and — from Phase 2 — the memory-mapped interactive store): :func:`rows_to_table` builds a
PyArrow table from dataclass rows, and :func:`flat_rows` denormalizes one record into the wide
``flat`` view.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, fields
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Final

import pyarrow as pa
import pyarrow.compute as _pc
import pyarrow.parquet as pq

from .model import (
    DEFAULT_BATCH_SIZE,
    FLAT_COLUMNS,
    TABLE_TYPES,
    ExtractedRecord,
    PropertyRow,
    flat_schema,
    schema_for,
)
from .parser import iter_records

# pyarrow.compute is under-typed in the stubs; route through Any (see filters.py for rationale).
pc: Any = _pc

# Ordered table names in the store: the four normalized tables plus the wide flat view.
STORE_TABLES: Final[list[str]] = [*TABLE_TYPES, "flat"]

# The interactive working store is Arrow IPC (Feather v2) — the one on-disk format pyarrow can
# memory-map for zero-copy, low-RAM random access. Parquet stays an export format only.
_STORE_SUFFIX: Final = ".arrow"


def rows_to_table(rows: list[Any], row_type: type) -> pa.Table:
    """Build a PyArrow table (explicit all-string schema) from a list of dataclass rows."""
    columns = {f.name: [getattr(r, f.name) for r in rows] for f in fields(row_type)}
    return pa.table(columns, schema=schema_for(row_type))


def flat_rows(rec: ExtractedRecord) -> list[dict[str, Any]]:
    """Denormalize one record into flat rows (one per property), with names concatenated."""
    assignor_names = "; ".join(a.name for a in rec.assignors if a.name) or None
    assignee_names = "; ".join(a.name for a in rec.assignees if a.name) or None
    # Roll up the assignors' dates: the latest signer date is the effective transaction date.
    execution_date = max(
        (a.execution_date for a in rec.assignors if a.execution_date), default=None
    )
    date_acknowledged = max(
        (a.date_acknowledged for a in rec.assignors if a.date_acknowledged), default=None
    )
    # The effective transfer date for time-axis work: prefer the (latest) execution date; fall back
    # to the recorded date when the filing omitted signer dates. ``date_source`` records which.
    recorded_date = rec.assignment.recorded_date or None
    transaction_date = execution_date or recorded_date
    date_source = "execution" if execution_date else ("recorded" if recorded_date else None)
    header = asdict(rec.assignment)
    base = {
        **header,
        "assignor_names": assignor_names,
        "assignee_names": assignee_names,
        "assignor_count": str(len(rec.assignors)),
        "assignee_count": str(len(rec.assignees)),
        "execution_date": execution_date,
        "date_acknowledged": date_acknowledged,
        "transaction_date": transaction_date,
        "date_source": date_source,
    }
    empty_prop = PropertyRow(
        reel_no=rec.assignment.reel_no,
        frame_no=rec.assignment.frame_no,
        invention_title=None,
        doc_country=None,
        doc_number=None,
        doc_kind=None,
        doc_name=None,
        doc_date=None,
    )
    props = rec.properties or [empty_prop]
    rows: list[dict[str, Any]] = []
    for p in props:
        rows.append(
            {
                **base,
                "invention_title": p.invention_title,
                "doc_country": p.doc_country,
                "doc_number": p.doc_number,
                "doc_kind": p.doc_kind,
                "doc_name": p.doc_name,
                "doc_date": p.doc_date,
            }
        )
    return rows


def _flat_table(rows: list[dict[str, Any]]) -> pa.Table:
    """Build the wide ``flat`` PyArrow table (fixed column order, all-string) from flat rows."""
    columns = {c: [r.get(c) for r in rows] for c in FLAT_COLUMNS}
    return pa.table(columns, schema=flat_schema())


# --------------------------------------------------------------------------------------
# Memory-mapped Arrow-IPC store
# --------------------------------------------------------------------------------------
@dataclass(slots=True)
class TableStore:
    """The five tables of a parsed dataset, held as (typically memory-mapped) PyArrow tables."""

    tables: dict[str, pa.Table]

    @property
    def names(self) -> list[str]:
        """Table names — the five canonical tables first, then any extra tables (e.g. a viewer)."""
        canonical = [n for n in STORE_TABLES if n in self.tables]
        extras = [n for n in self.tables if n not in STORE_TABLES]
        return canonical + extras

    def table(self, name: str) -> pa.Table:
        """Return one table by name (raises ``KeyError`` if absent)."""
        return self.tables[name]

    def row_counts(self) -> dict[str, int]:
        """Return the number of rows in each table."""
        return {name: self.tables[name].num_rows for name in self.names}

    def select_columns(self, columns: dict[str, list[str]]) -> TableStore:
        """Return a store projected to the given per-table columns (zero-copy).

        For a table present in ``columns``, only the named columns (that exist) are kept, in the
        given order; a table absent from ``columns`` keeps all columns. Tables mapped to an empty
        list are dropped entirely. Projection is a metadata-only view over the same buffers.
        """
        projected: dict[str, pa.Table] = {}
        for name, table in self.tables.items():
            if name not in columns:
                projected[name] = table
                continue
            wanted = [c for c in columns[name] if c in table.column_names]
            if wanted:
                projected[name] = table.select(wanted)
            # empty selection -> drop this table
        return TableStore(projected)


def _remove_partial_store(sinks: dict[str, _ArrowSink], store_dir: Path) -> None:
    """Close sinks best-effort and delete their files after a failed (or interrupted) parse.

    A mid-parse failure must not leave footer-less ``.arrow`` files behind: ``is_dataset_dir``
    would report the directory as a dataset while ``open_store`` fails on it.
    """
    for name, sink in sinks.items():
        with contextlib.suppress(pa.ArrowException, OSError):
            sink.close()
        (store_dir / f"{name}{_STORE_SUFFIX}").unlink(missing_ok=True)


class _ArrowSink:
    """Streaming Arrow-IPC (Feather v2) writer for one table; writes batches to one file."""

    def __init__(self, out_path: Path, schema: pa.Schema) -> None:
        self._sink = pa.OSFile(str(out_path), "wb")
        self._writer = pa.ipc.new_file(self._sink, schema)
        self.row_count = 0

    def write(self, table: pa.Table) -> None:
        if table.num_rows == 0:
            return
        self._writer.write_table(table)
        self.row_count += table.num_rows

    def close(self) -> None:
        self._writer.close()
        self._sink.close()


def parse_to_store(  # noqa: PLR0913, PLR0912 - clear keyword-only options + table-skip branches
    source: Path,
    store_dir: Path,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int | None = None,
    tables: set[str] | None = None,
    progress: Callable[[int], None] | None = None,
    progress_interval: int | None = None,
) -> TableStore:
    """Stream-parse ``source`` (``.xml``/``.zip``) into a memory-mapped Arrow-IPC store.

    Writes one ``<table>.arrow`` file per table in ``store_dir``, flushing every ``batch_size``
    records so peak memory stays flat on multi-GB inputs, then reopens them memory-mapped.

    Args:
        source: USPTO assignment ``.xml`` or ``.zip`` file.
        store_dir: Directory to write the Arrow-IPC files into (created if missing).
        batch_size: Records buffered before each flush.
        limit: Stop after this many assignment records (``None`` = parse all). Useful for a fast
            preview of a huge file.
        tables: Only build these tables (``None`` = all). Skipping the wide ``flat`` table (as large
            as ``properties``) when it is not needed roughly halves the work.
        progress: Optional callback invoked with the running record count.
        progress_interval: How often (in records) to call ``progress`` — independent of
            ``batch_size``. Defaults to ``batch_size``. The final count is always reported.

    Returns:
        A :class:`TableStore` of the freshly written, memory-mapped tables.
    """
    interval = progress_interval or batch_size
    wanted = (
        set(STORE_TABLES) if tables is None else (tables & set(STORE_TABLES)) or {"assignments"}
    )
    build_flat = "flat" in wanted
    store_dir.mkdir(parents=True, exist_ok=True)

    schemas: dict[str, pa.Schema] = {
        n: schema_for(TABLE_TYPES[n]) for n in TABLE_TYPES if n in wanted
    }
    if build_flat:
        schemas["flat"] = flat_schema()
    sinks = {
        name: _ArrowSink(store_dir / f"{name}{_STORE_SUFFIX}", schemas[name]) for name in schemas
    }

    buffers: dict[str, list[Any]] = {name: [] for name in TABLE_TYPES if name in wanted}
    flat_buffer: list[dict[str, Any]] = []
    seen = 0

    def flush() -> None:
        for name, row_type in TABLE_TYPES.items():
            if buffers.get(name):
                sinks[name].write(rows_to_table(buffers[name], row_type))
                buffers[name].clear()
        if flat_buffer:
            sinks["flat"].write(_flat_table(flat_buffer))
            flat_buffer.clear()

    # Iterate an explicit handle so an early ``limit`` break closes the underlying lxml/zip
    # streams deterministically (a suspended parser cleaned up at GC time raises unraisably).
    records = iter_records(source)
    try:
        try:
            for rec in records:
                if "assignments" in buffers:
                    buffers["assignments"].append(rec.assignment)
                if "assignors" in buffers:
                    buffers["assignors"].extend(rec.assignors)
                if "assignees" in buffers:
                    buffers["assignees"].extend(rec.assignees)
                if "properties" in buffers:
                    buffers["properties"].extend(rec.properties)
                if build_flat:
                    flat_buffer.extend(flat_rows(rec))
                seen += 1
                if seen % batch_size == 0:
                    flush()
                if progress is not None and seen % interval == 0:
                    progress(seen)
                if limit is not None and seen >= limit:
                    break
        finally:
            records.close()
        flush()
        for sink in sinks.values():
            sink.close()
    except BaseException:
        _remove_partial_store(sinks, store_dir)
        raise
    # Report the final count, unless the loop already reported it on an exact interval boundary.
    if progress is not None and seen % interval != 0:
        progress(seen)
    return open_store(store_dir)


def open_store(store_dir: Path) -> TableStore:
    """Open an existing Arrow-IPC store, memory-mapping each table for low, flat RAM use.

    Args:
        store_dir: Directory holding ``<table>.arrow`` files written by :func:`parse_to_store`.

    Returns:
        A :class:`TableStore` of memory-mapped tables (only the tables present are loaded).

    Raises:
        FileNotFoundError: If none of the expected ``<table>.arrow`` files exist.
    """
    tables: dict[str, pa.Table] = {}
    for name in STORE_TABLES:
        path = store_dir / f"{name}{_STORE_SUFFIX}"
        if path.is_file():
            # memory_map gives zero-copy, page-on-demand access; the returned table holds a
            # reference to the mapping, so it stays valid without keeping the file object around.
            source = pa.memory_map(str(path), "r")
            tables[name] = pa.ipc.open_file(source).read_all()
    if not tables:
        raise FileNotFoundError(f"no Arrow-IPC store tables found in {store_dir}")
    return TableStore(tables)


def open_parquet_store(store_dir: Path) -> TableStore:
    """Open a dataset previously saved as Parquet (``<table>.parquet`` per table).

    Unlike the Arrow store this reads (decompresses) into memory rather than memory-mapping, but
    lets a processed dataset be reopened directly and shared with other Parquet tools.

    Raises:
        FileNotFoundError: If no ``<table>.parquet`` files exist in ``store_dir``.
    """
    tables: dict[str, pa.Table] = {}
    for name in STORE_TABLES:
        path = store_dir / f"{name}.parquet"
        if path.is_file():
            tables[name] = pq.read_table(str(path))  # pyright: ignore[reportUnknownMemberType]
    if not tables:
        raise FileNotFoundError(f"no Parquet store tables found in {store_dir}")
    return TableStore(tables)


def is_dataset_dir(store_dir: Path) -> bool:
    """Whether ``store_dir`` holds a saved dataset (Arrow or Parquet store tables).

    True when :func:`open_dataset` could open it, False for any other folder (e.g. one holding raw
    XML/ZIP files) — lets a caller tell "already-parsed dataset" from "folder of inputs to parse".
    """
    return any(
        (store_dir / f"{name}{_STORE_SUFFIX}").is_file()
        or (store_dir / f"{name}.parquet").is_file()
        for name in STORE_TABLES
    )


def open_dataset(store_dir: Path) -> TableStore:
    """Open a saved dataset directory, auto-detecting Arrow (``.arrow``) or Parquet (``.parquet``).

    Arrow is preferred (memory-mapped, low RAM); Parquet is used if no Arrow tables are present.

    Raises:
        FileNotFoundError: If neither Arrow nor Parquet tables are found.
    """
    if any((store_dir / f"{name}{_STORE_SUFFIX}").is_file() for name in STORE_TABLES):
        return open_store(store_dir)
    if any((store_dir / f"{name}.parquet").is_file() for name in STORE_TABLES):
        return open_parquet_store(store_dir)
    raise FileNotFoundError(f"no Arrow (.arrow) or Parquet (.parquet) tables in {store_dir}")


def dataset_columns(store_dir: Path) -> dict[str, list[str]]:
    """Read the column names of each table in a saved dataset — schemas only, no data loaded.

    Lets template validation see a processed dataset's *actual* schema (e.g. the ``cpc_codes`` /
    ``*_canonical`` columns an earlier pipeline attached) instead of assuming the fresh-parse
    schema. Returns ``{}`` for a directory holding no dataset tables.
    """
    columns: dict[str, list[str]] = {}
    for name in STORE_TABLES:
        arrow_path = store_dir / f"{name}{_STORE_SUFFIX}"
        parquet_path = store_dir / f"{name}.parquet"
        if arrow_path.is_file():
            with pa.memory_map(str(arrow_path), "r") as source:
                columns[name] = list(pa.ipc.open_file(source).schema.names)
        elif parquet_path.is_file():
            schema: Any = pq.read_schema(str(parquet_path))  # pyright: ignore[reportUnknownMemberType]
            columns[name] = list(schema.names)
    return columns


TABLE_FILE_SUFFIXES: Final = (".parquet", ".arrow", ".feather", ".csv")


def stringify_column(column: Any) -> Any:
    """Cast one column to string for the generic viewer (lists joined with ``"; "``)."""
    col: Any = column.combine_chunks()
    if pa.types.is_string(col.type):
        return col
    if pa.types.is_list(col.type) or pa.types.is_large_list(col.type):
        joined = [
            None if value is None else "; ".join("" if v is None else str(v) for v in value)
            for value in col.to_pylist()
        ]
        return pa.array(joined, type=pa.string())
    try:
        return pc.cast(col, pa.string())
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError):  # structs/maps/etc. → Python str
        return pa.array([None if v is None else str(v) for v in col.to_pylist()], type=pa.string())


def read_table_file(path: Path) -> pa.Table:
    """Read a single ``.parquet``/``.arrow``/``.feather``/``.csv`` file into an all-string table.

    Every column is cast to string so the interactive viewer's filter/sort/model (built for text)
    work uniformly; values render and export exactly as shown. Type-preserving round-trips are not
    the goal — this is for viewing arbitrary files and converting them to CSV/JSON/Excel/etc.

    Raises:
        ValueError: For an unsupported file extension.
        FileNotFoundError: If ``path`` does not exist.
    """
    if not path.is_file():
        raise FileNotFoundError(f"file not found: {path}")
    suffix = path.suffix.lower()
    table: Any  # pyarrow readers are under-typed in the stubs; route the result through Any
    if suffix == ".parquet":
        table = pq.read_table(str(path))  # pyright: ignore[reportUnknownMemberType]
    elif suffix in (".arrow", ".feather"):
        with pa.OSFile(str(path), "rb") as handle:
            table = pa.ipc.open_file(handle).read_all()  # Arrow IPC (Feather v2)
    elif suffix == ".csv":
        import pyarrow.csv as pa_csv  # noqa: PLC0415 - lazy; only for CSV

        table = pa_csv.read_csv(str(path))
    else:
        raise ValueError(f"unsupported file type {suffix!r} (use {', '.join(TABLE_FILE_SUFFIXES)})")
    result: pa.Table = pa.table(
        {name: stringify_column(table.column(name)) for name in table.column_names}
    )
    return result


def match_input_files(
    folder: Path,
    pattern: str,
    suffixes: Sequence[str],
    *,
    recursive: bool = True,
) -> list[Path]:
    """Files under ``folder`` whose name matches ``pattern``, restricted to ``suffixes``.

    ``pattern`` is treated as a shell glob (``fnmatch`` on the file name) when it contains a glob
    metacharacter (``* ? [``) — e.g. ``*_flat*`` or ``*.parquet``; otherwise it is a
    case-insensitive substring — e.g. ``_flat`` matches ``daily_flat.parquet``. An empty pattern
    matches every file of an accepted suffix. Suffix matching is case-insensitive. The result is
    de-duplicated and sorted. Pure (filesystem + string only) so it is unit-testable without Qt.
    """
    accepted = {s.lower() for s in suffixes}
    needle = pattern.lower()
    is_glob = any(ch in pattern for ch in "*?[")
    candidates = folder.rglob("*") if recursive else folder.glob("*")
    matches: set[Path] = set()
    for candidate in candidates:
        if not candidate.is_file() or candidate.suffix.lower() not in accepted:
            continue
        name = candidate.name.lower()
        if fnmatch(name, needle) if is_glob else (needle in name):
            matches.add(candidate)
    return sorted(matches)


def file_columns(path: Path) -> list[str]:
    """Return the column names of a single data file — schema only, no data materialized.

    The cheap counterpart to :func:`read_table_file` for validation: lets a processed file input
    contribute its real schema (``cpc_codes``/``*_canonical`` an earlier pipeline attached) without
    reading the whole table. Mirrors :func:`dataset_columns` for the single-file case.

    Raises:
        ValueError: For an unsupported file extension.
        FileNotFoundError: If ``path`` does not exist.
    """
    if not path.is_file():
        raise FileNotFoundError(f"file not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        schema: Any = pq.read_schema(str(path))  # pyright: ignore[reportUnknownMemberType]
        return list(schema.names)
    if suffix in (".arrow", ".feather"):
        with pa.memory_map(str(path), "r") as source:
            return list(pa.ipc.open_file(source).schema.names)
    if suffix == ".csv":
        import pyarrow.csv as pa_csv  # noqa: PLC0415 - lazy; only for CSV

        reader: Any = pa_csv.open_csv(str(path))  # reads only the header to resolve the schema
        return list(reader.schema.names)
    raise ValueError(f"unsupported file type {suffix!r} (use {', '.join(TABLE_FILE_SUFFIXES)})")
