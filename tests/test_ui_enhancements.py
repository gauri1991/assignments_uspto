"""Headless tests for the enhancement round: categorical dropdowns, smart operators, AND/OR,
saved queries, recent files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pytestqt")

from uspto_assignments import Query, parse_to_store
from uspto_assignments_ui.app import create_app
from uspto_assignments_ui.settings import QueryStore, RecentStore
from uspto_assignments_ui.widgets.filter_bar import FilterBar
from uspto_assignments_ui.widgets.table_panel import TablePanel

FIXTURE = Path(__file__).parent / "fixtures" / "sample_assignment.xml"


def test_filter_bar_populates_categorical_dropdown(qtbot: Any) -> None:
    create_app([])

    def provider(column: str) -> list[str] | None:
        return ["A0", "B2", "X0"] if column == "kind" else None

    bar = FilterBar(["kind", "title"], distinct_provider=provider)
    qtbot.addWidget(bar)
    bar._column.setCurrentText("kind")
    assert [bar._value.itemText(i) for i in range(bar._value.count())] == ["A0", "B2", "X0"]
    assert bar._op.currentData() == "equals"  # categorical -> Equals

    bar._column.setCurrentText("title")
    assert bar._value.count() == 0  # high-cardinality -> free text
    assert bar._op.currentData() == "contains"


def test_filter_bar_smart_operator_for_dates(qtbot: Any) -> None:
    create_app([])
    bar = FilterBar(["recorded_date", "name"])
    qtbot.addWidget(bar)
    bar._column.setCurrentText("recorded_date")
    assert bar._op.currentData() == "in_range"


def test_table_panel_and_or_combine(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    store = parse_to_store(FIXTURE, tmp_path / "store")
    panel = TablePanel(store.table("properties"))
    qtbot.addWidget(panel)
    bar = panel._filter_bar

    for value in ("X0", "B2"):
        bar._column.setCurrentText("doc_kind")
        bar._op.setCurrentIndex(1)  # Equals
        bar._value.setCurrentText(value)
        bar._add_clause()

    bar._combine.setCurrentIndex(1)  # Match any (OR)
    assert len(panel.current_view_rows()) == 3  # 2 X0 + 1 B2
    bar._combine.setCurrentIndex(0)  # Match all (AND)
    assert len(panel.current_view_rows()) == 0  # no row is both X0 and B2


def test_table_panel_query_roundtrip(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    store = parse_to_store(FIXTURE, tmp_path / "store")
    panel = TablePanel(store.table("properties"))
    qtbot.addWidget(panel)
    bar = panel._filter_bar
    bar._column.setCurrentText("doc_kind")
    bar._op.setCurrentIndex(1)
    bar._value.setCurrentText("B2")
    bar._add_clause()

    query = panel.to_query("granted", "properties")
    assert query.clauses[0].value == "B2"

    fresh = TablePanel(store.table("properties"))
    qtbot.addWidget(fresh)
    fresh.apply_query(query)
    assert len(fresh.current_view_rows()) == 1


def test_recent_store_dedup_and_cap(tmp_path: Path) -> None:
    store = RecentStore(tmp_path / "recent.json", limit=2)
    store.add("/a", "file")
    store.add("/b", "dataset")
    store.add("/a", "file")  # moves /a to front, de-duplicated
    assert [(e.path, e.kind) for e in store.load()] == [("/a", "file"), ("/b", "dataset")]
    store.add("/c", "file")  # cap = 2, oldest (/b) drops
    assert [e.path for e in store.load()] == ["/c", "/a"]
    store.clear()
    assert store.load() == []


def test_query_store_add_replace_delete(tmp_path: Path) -> None:
    store = QueryStore(tmp_path / "queries.json")
    store.add(Query(name="q", table="properties", quick_search="one"))
    store.add(Query(name="q", table="properties", quick_search="two"))  # replaces same name
    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].quick_search == "two"
    store.delete("q")
    assert store.load() == []
