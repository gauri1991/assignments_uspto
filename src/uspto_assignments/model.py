"""Row types and table schemas for USPTO patent-assignment data.

USPTO patent-assignment XML is deeply nested and one-to-many: each ``patent-assignment``
carries one assignment-record header plus repeating assignors, assignees, and patent
properties (each property carrying one or more ``document-id`` blocks). We normalize that into
four tables — ``assignments``, ``assignors``, ``assignees``, ``properties`` — keyed by
``reel_no`` + ``frame_no``, plus a wide ``flat`` join built by :mod:`uspto_assignments.tables`.

Every column is a nullable string: USPTO numbers carry significant leading zeros and dates may
be partial (``YYYYMMDD``, ``YYYY0000``), so values are preserved verbatim rather than coerced.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Final

import pyarrow as pa

# Excel hard limits: 1,048,576 rows total, so 1,048,575 usable data rows below the header.
EXCEL_MAX_ROWS: Final = 1_048_576
# Records buffered before each streaming flush (Parquet, Excel, and the Arrow-IPC store).
DEFAULT_BATCH_SIZE: Final = 5_000


@dataclass(slots=True)
class Assignment:
    """One assignment-record header row, keyed by reel/frame."""

    reel_no: str | None
    frame_no: str | None
    last_update_date: str | None
    recorded_date: str | None
    purge_indicator: str | None
    page_count: str | None
    conveyance_text: str | None
    correspondent_name: str | None
    correspondent_address_1: str | None
    correspondent_address_2: str | None
    correspondent_address_3: str | None
    correspondent_address_4: str | None


@dataclass(slots=True)
class Assignor:
    """One assignor row, linked to its assignment by reel/frame."""

    reel_no: str | None
    frame_no: str | None
    name: str | None
    execution_date: str | None
    date_acknowledged: str | None


@dataclass(slots=True)
class Assignee:
    """One assignee row, linked to its assignment by reel/frame."""

    reel_no: str | None
    frame_no: str | None
    name: str | None
    address_1: str | None
    address_2: str | None
    city: str | None
    state: str | None
    country_name: str | None
    postcode: str | None


@dataclass(slots=True)
class PropertyRow:
    """One (patent-property x document-id) row, linked by reel/frame."""

    reel_no: str | None
    frame_no: str | None
    invention_title: str | None
    doc_country: str | None
    doc_number: str | None
    doc_kind: str | None
    doc_name: str | None
    doc_date: str | None


@dataclass(slots=True)
class ExtractedRecord:
    """The fully normalized set of rows produced from one ``patent-assignment``."""

    assignment: Assignment
    assignors: list[Assignor]
    assignees: list[Assignee]
    properties: list[PropertyRow]


# Table name -> its row dataclass. Field order == column order in every output.
TABLE_TYPES: Final[dict[str, type]] = {
    "assignments": Assignment,
    "assignors": Assignor,
    "assignees": Assignee,
    "properties": PropertyRow,
}

# Column order for the wide ``flat`` view: assignment header, then rollups, then property fields.
# ``execution_date``/``date_acknowledged`` roll up the assignors' dates; ``transaction_date`` is the
# effective transfer date for time-axis work — the latest ``execution_date``, or ``recorded_date``
# when no signer date was filed — with ``date_source`` recording which (``execution``/``recorded``).
FLAT_COLUMNS: Final[list[str]] = [
    *(f.name for f in fields(Assignment)),
    "assignor_names",
    "assignee_names",
    "assignor_count",
    "assignee_count",
    "execution_date",
    "date_acknowledged",
    "transaction_date",
    "date_source",
    "invention_title",
    "doc_country",
    "doc_number",
    "doc_kind",
    "doc_name",
    "doc_date",
]


def columns_for(table_name: str) -> list[str]:
    """Return the column names of a store table (``flat`` or a normalized table).

    Known statically from the schema — no parsing needed — so the UI can offer field selection
    before a file is opened.
    """
    if table_name == "flat":
        return list(FLAT_COLUMNS)
    return [f.name for f in fields(TABLE_TYPES[table_name])]


def schema_for(row_type: type) -> pa.Schema:
    """Return an all-nullable-string PyArrow schema with one field per dataclass column."""
    return pa.schema([pa.field(f.name, pa.string()) for f in fields(row_type)])


def flat_schema() -> pa.Schema:
    """Return the all-string PyArrow schema for the wide ``flat`` view."""
    return pa.schema([pa.field(name, pa.string()) for name in FLAT_COLUMNS])
