"""Tests for saved-query serialization (uspto_assignments.query)."""

from __future__ import annotations

from pathlib import Path

from uspto_assignments import FilterClause, Query, dump_queries, load_queries


def test_query_roundtrips_through_json(tmp_path: Path) -> None:
    query = Query(
        name="granted B2",
        table="properties",
        combine="or",
        quick_search="US",
        clauses=[
            FilterClause(column="doc_kind", op="equals", value="B2"),
            FilterClause(column="doc_country", op="equals", value="US"),
        ],
        sort=("doc_number", True),
    )
    path = tmp_path / "queries.json"
    dump_queries([query], path)

    loaded = load_queries(path)
    assert len(loaded) == 1
    restored = loaded[0]
    assert restored.name == "granted B2"
    assert restored.table == "properties"
    assert restored.combine == "or"
    assert restored.quick_search == "US"
    assert [(c.column, c.op, c.value) for c in restored.clauses] == [
        ("doc_kind", "equals", "B2"),
        ("doc_country", "equals", "US"),
    ]
    assert restored.sort == ("doc_number", True)


def test_load_queries_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_queries(tmp_path / "nope.json") == []


def test_query_without_sort_or_clauses(tmp_path: Path) -> None:
    path = tmp_path / "q.json"
    dump_queries([Query(name="all", table="assignments")], path)
    restored = load_queries(path)[0]
    assert restored.sort is None
    assert restored.clauses == []
