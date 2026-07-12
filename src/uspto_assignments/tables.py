"""Turn extracted records into columnar tables.

This module holds the record→table transforms shared by every output path (Parquet, Excel,
and — from Phase 2 — the memory-mapped interactive store): :func:`rows_to_table` builds a
PyArrow table from dataclass rows, and :func:`flat_rows` denormalizes one record into the wide
``flat`` view.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Final

import pyarrow as pa
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
        """Table names in canonical order."""
        return [n for n in STORE_TABLES if n in self.tables]

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
