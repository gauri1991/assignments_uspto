"""Headless tests for the Phase 4 interactivity: parse worker, filter bar, table panel.

These reach into a few private widget fields to simulate user input (typing a value, choosing an
operator) without a real mouse/keyboard — acceptable in tests to exercise the wiring.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pytestqt")

from uspto_assignments import exporters, open_store, parse_to_store
from uspto_assignments_ui.app import create_app
from uspto_assignments_ui.widgets.export_dialog import ExportDialog
from uspto_assignments_ui.widgets.filter_bar import FilterBar
from uspto_assignments_ui.widgets.table_panel import TablePanel
from uspto_assignments_ui.workers import ParseWorker

FIXTURE = Path(__file__).parent / "fixtures" / "sample_assignment.xml"


def test_parse_worker_emits_finished_with_store(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    worker = ParseWorker(FIXTURE, tmp_path / "store")
    results: list[Any] = []
    worker.finished.connect(results.append)  # pyright: ignore[reportUnknownMemberType]  # Qt signal
    worker.run()  # run synchronously on this thread (deterministic)
    assert len(results) == 1
    assert results[0].row_counts()["properties"] == 4


def test_parse_worker_emits_failed_on_bad_input(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    zip_no_xml = tmp_path / "empty.zip"
    with zipfile.ZipFile(zip_no_xml, "w") as zf:
        zf.writestr("readme.txt", "nope")
    worker = ParseWorker(zip_no_xml, tmp_path / "store")
    errors: list[str] = []
    worker.failed.connect(errors.append)  # pyright: ignore[reportUnknownMemberType]  # Qt signal
    worker.run()
    assert errors and "no .xml member" in errors[0]


def test_filter_bar_builds_clauses(qtbot: Any) -> None:
    create_app([])
    bar = FilterBar(["reel_no", "name"])
    qtbot.addWidget(bar)
    bar._column.setCurrentText("name")
    bar._op.setCurrentIndex(0)  # "Contains"
    bar._value.setCurrentText("ACME")
    bar._add_clause()
    clauses = bar.clauses()
    assert len(clauses) == 1
    assert (clauses[0].column, clauses[0].op, clauses[0].value) == ("name", "contains", "ACME")


def test_table_panel_filter_narrows_view(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    store = parse_to_store(FIXTURE, tmp_path / "store")
    panel = TablePanel(store.table("properties"))
    qtbot.addWidget(panel)
    assert len(panel.current_view_rows()) == 4

    bar = panel._filter_bar
    bar._column.setCurrentText("doc_kind")
    bar._op.setCurrentIndex(1)  # "Equals"
    bar._value.setCurrentText("B2")
    bar._add_clause()

    rows = panel.current_view_rows()
    assert len(rows) == 1
    assert store.table("properties").column("doc_number")[rows[0]].as_py() == "10987654"


def test_table_panel_reopens_from_disk_store(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    parse_to_store(FIXTURE, tmp_path / "store")
    store = open_store(tmp_path / "store")  # memory-mapped reopen
    panel = TablePanel(store.table("assignors"))
    qtbot.addWidget(panel)
    assert len(panel.current_view_rows()) == 3


def test_export_dialog_reports_format_and_scope(qtbot: Any) -> None:
    create_app([])
    dialog = ExportDialog(total_rows=100, view_rows=10, selected_rows=3)
    qtbot.addWidget(dialog)
    assert dialog.selected_scope() == "selected"  # defaults to most specific
    dialog._format.setCurrentIndex(2)  # CSV
    assert dialog.selected_format() == "csv"
    no_scope = ExportDialog(show_scope=False)
    qtbot.addWidget(no_scope)
    assert no_scope.selected_scope() == "all"


def test_export_filtered_scope_from_panel(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    store = parse_to_store(FIXTURE, tmp_path / "store")
    panel = TablePanel(store.table("properties"))
    qtbot.addWidget(panel)
    bar = panel._filter_bar
    bar._column.setCurrentText("doc_kind")
    bar._op.setCurrentIndex(1)  # Equals
    bar._value.setCurrentText("B2")
    bar._add_clause()

    out = tmp_path / "filtered.csv"
    written = exporters.export(panel.table, out, "csv", rows=panel.current_view_rows())
    assert written == 1
    assert "10987654" in out.read_text(encoding="utf-8")
