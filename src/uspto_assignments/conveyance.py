"""Conveyance taxonomy: map raw ``conveyance_text`` to a curated ``conveyance_type``.

Replaces brittle substring filtering with an ordered rule table. Classification runs once per
**distinct** text (the corpus has only a few hundred distinct conveyance strings across millions of
rows), so this is effectively free — and it is the biggest cheap row cut in the buyer pipeline:
security interests, releases, and name changes are excluded *by type*, not by fragile substrings.
"""

from __future__ import annotations

import re
from typing import Any, Literal

import pyarrow as pa
import pyarrow.compute as _pc_module

# pyarrow.compute is under-typed in pyarrow-stubs; route through Any (see filters.py for rationale).
pc: Any = _pc_module

ConveyanceType = Literal[
    "assignment",
    "security_interest",
    "release",
    "merger",
    "name_change",
    "license",
    "correction",
    "nunc_pro_tunc",
    "other",
]

# Ordered rules — FIRST match wins, so the specific kinds are tested before the generic
# "assignment" catch-all (a nunc-pro-tunc or corrective *assignment* must not classify as plain
# assignment, and "ASSIGNMENT ... FOR SECURITY" is a security interest, not a sale). Release
# precedes security_interest: release texts almost always name the security interest they release
# ("RELEASE OF SECURITY INTEREST"), while genuine security grants don't mention release/termination.
_RULES: list[tuple[re.Pattern[str], ConveyanceType]] = [
    (re.compile(r"\bRELEASE\b|TERMINATION\s+(AND\s+RELEASE|OF\s+SECURITY)"), "release"),
    (
        re.compile(r"SECURITY\s+(INTEREST|AGREEMENT)|FOR\s+SECURITY|\bLIEN\b|COLLATERAL"),
        "security_interest",
    ),
    (re.compile(r"NUNC\s+PRO\s+TUNC"), "nunc_pro_tunc"),
    (re.compile(r"CORRECTIV|CORRECTION|TO\s+CORRECT"), "correction"),
    (re.compile(r"\bMERGER\b|CHANGE\s+OF\s+NAME\s+AND\s+MERGER"), "merger"),
    (re.compile(r"CHANGE\s+OF\s+NAME|NAME\s+CHANGE"), "name_change"),
    (re.compile(r"\bLICENSE\b|\bLICENCE\b"), "license"),
    (re.compile(r"ASSIGN"), "assignment"),  # ASSIGNMENT / ASSIGNORS INTEREST / ASSIGNOR'S ...
]

# The buyer pipeline keeps these types by default; nunc-pro-tunc and corrective assignments are
# often the only record of a genuine transfer, so they are dialable-in rather than hard-excluded.
DEFAULT_KEPT_TYPES: frozenset[str] = frozenset({"assignment"})
TRANSFER_LIKE_TYPES: frozenset[str] = frozenset({"assignment", "nunc_pro_tunc", "correction"})


def classify_conveyance(text: str | None) -> ConveyanceType:
    """Classify one raw conveyance string (case-insensitive; first matching rule wins)."""
    if not text:
        return "other"
    upper = text.upper()
    for pattern, conveyance_type in _RULES:
        if pattern.search(upper):
            return conveyance_type
    return "other"


def conveyance_type_column(
    table: pa.Table, column: str = "conveyance_text", target: str = "conveyance_type"
) -> pa.Table:
    """Return ``table`` with a ``target`` column of conveyance types (distinct-value classified)."""
    source: Any = table.column(column).combine_chunks()
    encoded: Any = source.dictionary_encode()
    mapped = [
        None if value is None else classify_conveyance(value)
        for value in encoded.dictionary.to_pylist()
    ]
    values: Any = pc.take(pa.array(mapped, type=pa.string()), encoded.indices)
    if target in table.column_names:
        table = table.drop_columns([target])
    return table.append_column(target, values)
