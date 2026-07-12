"""Vectorized filtering and sorting over the columnar tables.

The interactive UI can face millions of rows (the ``properties`` table hit ~4M on a real bulk
file). A Qt ``QSortFilterProxyModel`` evaluates a Python predicate *per row* and would freeze,
so instead we filter in ``pyarrow.compute`` (vectorized C++) and return the **row indices** that
pass. The Qt model then pages through that index array, querying only the visible cells.

Every column is a string, so text predicates apply uniformly and date ranges compare
lexicographically on the ``YYYYMMDD`` form (which orders correctly).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import pyarrow as pa
import pyarrow.compute as _pc_module

# ``pyarrow.compute`` is under-typed in pyarrow-stubs: its functions accept Python scalars and
# ChunkedArrays that the stub overloads omit, and results are ``Array[Unknown]``. Routing calls
# through an ``Any`` view contains those well-known false positives here (this module is the only
# heavy compute user) instead of disabling type rules project-wide, which would mask real errors.
pc: Any = _pc_module

FilterOp = Literal[
    "contains", "equals", "not_equals", "starts_with", "not_empty", "is_empty", "in_range"
]
# Operators that ignore the clause value (they test presence, not content).
_VALUELESS_OPS: frozenset[FilterOp] = frozenset({"not_empty", "is_empty"})


@dataclass(frozen=True, slots=True)
class FilterClause:
    """One filter condition on a single column.

    Args:
        column: Column name to test.
        op: The comparison operator.
        value: Operand for ``contains``/``equals``/``starts_with``, and the lower bound for
            ``in_range``.
        value2: Upper bound for ``in_range`` (ignored otherwise).
        case_sensitive: Whether text comparisons are case-sensitive (default: no).
    """

    column: str
    op: FilterOp
    value: str = ""
    value2: str = ""
    case_sensitive: bool = False


def _clause_mask(table: pa.Table, clause: FilterClause) -> pa.Array[Any]:
    """Return a boolean mask (nulls treated as non-matching) for one clause."""
    col = table.column(clause.column)
    ignore_case = not clause.case_sensitive
    match clause.op:
        case "contains":
            mask = pc.match_substring(col, clause.value, ignore_case=ignore_case)
        case "starts_with":
            mask = pc.starts_with(col, clause.value, ignore_case=ignore_case)
        case "equals":
            if ignore_case:
                mask = pc.equal(pc.utf8_lower(col), clause.value.lower())
            else:
                mask = pc.equal(col, clause.value)
        case "not_equals":
            # Null-safe: a null value is "not equal" to the operand (kept), matching intuition
            # for exclusion filters like ``purge_indicator not_equals Y``.
            if ignore_case:
                mask = pc.or_kleene(
                    pc.is_null(col), pc.not_equal(pc.utf8_lower(col), clause.value.lower())
                )
            else:
                mask = pc.or_kleene(pc.is_null(col), pc.not_equal(col, clause.value))
        case "not_empty":
            mask = pc.and_(pc.is_valid(col), pc.greater(pc.utf8_length(col), 0))
        case "is_empty":
            # Kleene OR so a null column value counts as empty (true OR null = true).
            mask = pc.or_kleene(pc.is_null(col), pc.equal(col, ""))
        case "in_range":
            mask = pc.and_(
                pc.greater_equal(col, clause.value),
                pc.less_equal(col, clause.value2),
            )
    return pc.fill_null(mask, False)


def _quick_mask(table: pa.Table, text: str) -> pa.Array[Any]:
    """Return a boolean mask matching ``text`` as a substring in any column (case-insensitive)."""
    combined: pa.Array[Any] | None = None
    for name in table.column_names:
        col_mask = pc.fill_null(
            pc.match_substring(table.column(name), text, ignore_case=True), False
        )
        combined = col_mask if combined is None else pc.or_(combined, col_mask)
    if combined is None:  # table with no columns
        return pa.array([True] * table.num_rows, type=pa.bool_())
    return combined


CombineMode = Literal["and", "or"]


def apply(
    table: pa.Table,
    clauses: Sequence[FilterClause] = (),
    *,
    quick_search: str | None = None,
    combine: CombineMode = "and",
) -> pa.Array[Any]:
    """Return the row indices of ``table`` matching the clauses (combined) AND the quick search.

    Args:
        table: The table to filter.
        clauses: Filter conditions (empty = match all).
        quick_search: Optional global substring matched (case-insensitive) against any column.
        combine: Whether clauses are AND-ed (match all) or OR-ed (match any). The quick search,
            when present, always further narrows the result (AND-ed on top).

    Returns:
        An ``int64`` array of matching row indices, in ascending row order.
    """
    if table.num_rows == 0:
        # Guard: pyarrow 25 compute kernels can SIGSEGV on edge-shaped empty/zero-chunk columns
        # (e.g. a column appended to a zeroed table). An empty table matches nothing — return early.
        return pa.array([], type=pa.int64())
    clause_mask: pa.Array[Any] | None = None
    for clause in clauses:
        this = _clause_mask(table, clause)
        if clause_mask is None:
            clause_mask = this
        elif combine == "and":
            clause_mask = pc.and_(clause_mask, this)
        else:
            clause_mask = pc.or_kleene(clause_mask, this)

    mask = clause_mask
    if quick_search:
        quick = _quick_mask(table, quick_search)
        mask = quick if mask is None else pc.and_(mask, quick)
    if mask is None:
        return pa.array(range(table.num_rows), type=pa.int64())
    return pc.indices_nonzero(mask).cast(pa.int64())


def distinct_values(table: pa.Table, column: str, *, max_unique: int = 200) -> list[str] | None:
    """Return the sorted distinct non-empty values of a column, or None if high-cardinality.

    Used to offer a value dropdown for categorical fields (e.g. conveyance-text, kind, state)
    while keeping free text for columns with too many distinct values (titles, doc numbers).
    ``count_distinct`` is checked first so a high-cardinality column is never fully materialized.
    """
    col = table.column(column)
    if pc.count_distinct(col).as_py() > max_unique:
        return None
    values = [v for v in pc.unique(col).to_pylist() if isinstance(v, str) and v]
    return sorted(values)


def sort_indices(table: pa.Table, column: str, *, ascending: bool = True) -> pa.Array[Any]:
    """Return row indices that order ``table`` by ``column`` (nulls last)."""
    if table.num_rows == 0:  # same empty-kernel guard as ``apply``
        return pa.array([], type=pa.int64())
    order = "ascending" if ascending else "descending"
    return pc.sort_indices(table, sort_keys=[(column, order)]).cast(pa.int64())


# A sort request: the column to order by and whether it is ascending.
SortSpec = tuple[str, bool]


def filter_sort(
    table: pa.Table,
    clauses: Sequence[FilterClause] = (),
    *,
    quick_search: str | None = None,
    combine: CombineMode = "and",
    sort: SortSpec | None = None,
) -> pa.Array[Any]:
    """Return row indices after applying filters/quick-search AND an optional sort.

    Filtering happens first (so we sort only the surviving rows); the sort is then applied
    within that subset and mapped back to original row indices — the exact index array a
    ``QAbstractTableModel`` view needs.
    """
    indices = apply(table, clauses, quick_search=quick_search, combine=combine)
    if sort is not None:
        column, ascending = sort
        order = sort_indices(table.take(indices), column, ascending=ascending)
        indices = indices.take(order)
    return indices


def is_valueless(op: FilterOp) -> bool:
    """Whether ``op`` ignores the clause value (presence tests like empty/not-empty)."""
    return op in _VALUELESS_OPS
