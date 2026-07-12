"""Tests for vectorized filtering and sorting (uspto_assignments.filters)."""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from uspto_assignments import filters
from uspto_assignments.filters import FilterClause, apply, sort_indices


def _table() -> pa.Table:
    return pa.table(
        {
            "name": ["ACME CORP", "acme llc", "Beta Inc", None, ""],
            "state": ["CA", "CA", "TX", "NY", "CA"],
            "date": ["20200101", "20211231", "20190615", "20250701", None],
        }
    )


def _indices(array: pa.Array[Any]) -> list[Any]:
    return array.to_pylist()


def test_no_clauses_returns_all_rows() -> None:
    assert _indices(filters.apply(_table())) == [0, 1, 2, 3, 4]


def test_contains_is_case_insensitive_by_default() -> None:
    clause = FilterClause(column="name", op="contains", value="acme")
    assert _indices(filters.apply(_table(), [clause])) == [0, 1]


def test_contains_case_sensitive() -> None:
    clause = FilterClause(column="name", op="contains", value="acme", case_sensitive=True)
    assert _indices(filters.apply(_table(), [clause])) == [1]


def test_equals_case_insensitive() -> None:
    clause = FilterClause(column="name", op="equals", value="acme corp")
    assert _indices(filters.apply(_table(), [clause])) == [0]


def test_starts_with() -> None:
    clause = FilterClause(column="name", op="starts_with", value="Beta")
    assert _indices(filters.apply(_table(), [clause])) == [2]


def test_not_empty_and_is_empty_partition_rows() -> None:
    not_empty = FilterClause(column="name", op="not_empty")
    is_empty = FilterClause(column="name", op="is_empty")
    assert _indices(filters.apply(_table(), [not_empty])) == [0, 1, 2]
    assert _indices(filters.apply(_table(), [is_empty])) == [3, 4]  # null and "" both empty


def test_in_range_on_lexicographic_dates() -> None:
    clause = FilterClause(column="date", op="in_range", value="20200101", value2="20211231")
    assert _indices(filters.apply(_table(), [clause])) == [0, 1]


def test_multiple_clauses_are_anded() -> None:
    clauses = [
        FilterClause(column="state", op="equals", value="CA"),
        FilterClause(column="name", op="contains", value="acme"),
    ]
    assert _indices(filters.apply(_table(), clauses)) == [0, 1]


def test_quick_search_matches_any_column() -> None:
    assert _indices(filters.apply(_table(), quick_search="TX")) == [2]
    assert _indices(filters.apply(_table(), quick_search="20250701")) == [3]


def test_combine_or_matches_any_clause() -> None:
    table = _table()
    clauses = [
        FilterClause(column="state", op="equals", value="TX"),
        FilterClause(column="name", op="contains", value="acme"),
    ]
    assert _indices(filters.apply(table, clauses, combine="and")) == []  # no row is both
    assert _indices(filters.apply(table, clauses, combine="or")) == [0, 1, 2]  # rows 0,1 acme; 2 TX


def test_distinct_values_returns_sorted_for_low_cardinality() -> None:
    assert filters.distinct_values(_table(), "state") == ["CA", "NY", "TX"]


def test_distinct_values_none_when_over_threshold() -> None:
    # 3 distinct states, threshold 2 -> None (treated as free-text)
    assert filters.distinct_values(_table(), "state", max_unique=2) is None


def test_distinct_values_skips_null_and_empty() -> None:
    assert filters.distinct_values(_table(), "name") == ["ACME CORP", "Beta Inc", "acme llc"]


def test_filter_sort_filters_then_sorts_within_subset() -> None:
    table = _table()
    # keep only state == CA (rows 0,1,4), then sort those by date ascending (nulls last)
    clauses = [FilterClause(column="state", op="equals", value="CA")]
    result = filters.filter_sort(table, clauses, sort=("date", True)).to_pylist()
    assert result == [0, 1, 4]  # 20200101, 20211231, then null(row4) last


def test_filter_sort_without_sort_is_just_filter() -> None:
    table = _table()
    clauses = [FilterClause(column="state", op="equals", value="CA")]
    assert filters.filter_sort(table, clauses).to_pylist() == [0, 1, 4]


def test_sort_indices_ascending_and_descending_null_last() -> None:
    table = _table()
    asc = filters.sort_indices(table, "date").to_pylist()
    assert asc[:3] == [2, 0, 1]  # 20190615, 20200101, 20211231
    assert asc[-1] == 4  # null sorts last
    desc = filters.sort_indices(table, "date", ascending=False).to_pylist()
    assert desc[0] == 3  # 20250701 first


def test_not_equals_is_null_safe_and_case_insensitive() -> None:
    table = pa.table({"purge_indicator": ["N", "Y", None, "y", ""]})
    keep = apply(table, [FilterClause("purge_indicator", "not_equals", "Y")])
    # keeps N, null, and "" — drops Y and y (case-insensitive by default)
    assert keep.to_pylist() == [0, 2, 4]
    keep_cs = apply(
        table, [FilterClause("purge_indicator", "not_equals", "Y", case_sensitive=True)]
    )
    assert keep_cs.to_pylist() == [0, 2, 3, 4]  # case-sensitive keeps lowercase "y"


def test_apply_and_sort_on_empty_table_return_empty() -> None:
    """Regression: pyarrow-25 kernels can SIGSEGV on zeroed tables — we guard before the kernel."""
    table = pa.table({"a": pa.array([], type=pa.string())})
    assert apply(table, [FilterClause("a", "equals", "x")]).to_pylist() == []
    assert apply(table, []).to_pylist() == []
    assert sort_indices(table, "a").to_pylist() == []
