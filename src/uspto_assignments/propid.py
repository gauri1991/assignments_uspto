"""Property-identity canonicalization: document type, normalized patent ids, canonical keys.

The ``properties`` table holds one row per **document id**, and one invention typically appears as
several rows in the same recording — its application (`kind X0`, 8-digit serial), its pre-grant
publication (`A1/A2/A9`, 11-digit), and its grant (`B*`, 7-digit). Counting distinct ``doc_number``
therefore counts *documents*, not properties. This module derives:

- ``doc_type`` ∈ ``application | publication | grant | unknown`` (from kind code + number shape);
- ``patent_id_normalized`` — the **grant** number formatted for the CPC join target (default:
  PatentsView convention — no country prefix, no leading zeros, letter prefixes ``D/PP/RE/H/T``
  kept). Non-grant rows get ``""`` so downstream grant-only routing is explicit;
- a canonical property key (application number preferred as the stable identity) — computed at the
  ledger stage by grouping same-recording rows that share an invention title.
"""

from __future__ import annotations

import re
from typing import Any, Literal

import pyarrow as pa
import pyarrow.compute as _pc_module

# pyarrow.compute is under-typed in pyarrow-stubs; route through Any (see filters.py for rationale).
pc: Any = _pc_module

DocType = Literal["application", "publication", "grant", "unknown"]
PatentIdFormat = Literal["patentsview", "raw"]

_PUBLICATION_KINDS = frozenset({"A1", "A2", "A9", "P1", "P4"})
_GRANT_PREFIXES = ("D", "PP", "RE", "H", "T")  # design / plant / reissue / SIR / defensive
_PUBLICATION_LENGTH = 11  # YYYYNNNNNNN
_MAX_GRANT_DIGITS = 8
_GRANT_NUMBER_RE = re.compile(r"^(?:US)?([A-Z]{0,2})0*(\d+)$")


def doc_type_for(  # noqa: PLR0911 - one return per document class
    doc_number: str | None, doc_kind: str | None
) -> DocType:
    """Derive the document type from the kind code, falling back to number shape."""
    number = (doc_number or "").strip().upper()
    kind = (doc_kind or "").strip().upper()
    digits = "".join(c for c in number if c.isdigit())
    if kind == "X0":
        return "application"  # USPTO assignment data uses X0 for application serials
    if kind.startswith("B"):
        return "grant"
    if kind in _PUBLICATION_KINDS and len(digits) == _PUBLICATION_LENGTH:
        return "publication"
    if kind == "A":  # legacy pre-2001 grants used kind A; publications are 11 digits
        return "grant" if len(digits) <= _MAX_GRANT_DIGITS else "publication"
    if number.startswith(_GRANT_PREFIXES) and digits:
        return "grant"
    if len(digits) == _PUBLICATION_LENGTH:
        return "publication"
    return "unknown"


def normalize_patent_id(
    doc_number: str | None, doc_type: str, fmt: PatentIdFormat = "patentsview"
) -> str:
    """Normalize a **grant** number to the CPC join target's convention (``""`` for non-grants).

    ``patentsview``: matches PatentsView ``patent_id`` — strip any ``US`` prefix and leading zeros
    from the numeric part, keep letter prefixes (``D``, ``PP``, ``RE``, ``H``, ``T``) uppercase.
    ``raw``: pass the trimmed uppercase number through unchanged.
    """
    if doc_type != "grant" or not doc_number:
        return ""
    number = doc_number.strip().upper()
    if fmt == "raw":
        return number
    match = _GRANT_NUMBER_RE.match(number)
    if match is None:
        return number  # unusual shape: pass through rather than corrupt
    prefix, digits = match.groups()
    return f"{prefix}{int(digits)}"


def add_doc_columns(table: pa.Table, *, fmt: PatentIdFormat = "patentsview") -> pa.Table:
    """Return ``properties`` with ``doc_type`` and ``patent_id_normalized`` columns appended.

    Classification runs once per distinct ``(doc_number, doc_kind)`` pair, then maps back — the
    same distinct-value pattern used across the pipeline.
    """
    numbers = table.column("doc_number").to_pylist()
    kinds = table.column("doc_kind").to_pylist()
    cache: dict[tuple[str | None, str | None], tuple[str, str]] = {}
    doc_types: list[str] = []
    norm_ids: list[str] = []
    for number, kind in zip(numbers, kinds, strict=True):
        key = (number, kind)
        hit = cache.get(key)
        if hit is None:
            doc_type = doc_type_for(number, kind)
            hit = (doc_type, normalize_patent_id(number, doc_type, fmt))
            cache[key] = hit
        doc_types.append(hit[0])
        norm_ids.append(hit[1])
    for name, values in (("doc_type", doc_types), ("patent_id_normalized", norm_ids)):
        if name in table.column_names:
            table = table.drop_columns([name])
        table = table.append_column(name, pa.array(values, type=pa.string()))
    return table
