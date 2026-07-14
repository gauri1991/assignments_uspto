"""Headless (offscreen) smoke tests for the PyQt6 UI layer.

These verify the model↔view↔store wiring and that the Metro stylesheet loads — the heavy logic
(parsing, filtering, exporting) is already covered by the core tests, so this stays minimal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pytestqt")

from PyQt6.QtCore import Qt

from uspto_assignments import filters, parse_to_store
from uspto_assignments.filters import FilterClause
from uspto_assignments_ui.app import create_app, load_stylesheet
from uspto_assignments_ui.main_window import MainWindow
from uspto_assignments_ui.models.arrow_table_model import ArrowTableModel

FIXTURE = Path(__file__).parent / "fixtures" / "sample_assignment.xml"


def test_stylesheet_is_flat_metro() -> None:
    qss = load_stylesheet().lower()
    assert "border-radius: 0px" in qss  # no rounded corners
    assert "#0078d7" in qss  # accent present
    # flat fills only — no Qt gradient functions (the word "gradient" may appear in comments)
    assert "qlineargradient" not in qss
    assert "qradialgradient" not in qss


def test_main_window_populates_one_tab_per_table(qtbot: Any, tmp_path: Path) -> None:
    create_app([])  # applies metro.qss to the (pytest-qt) QApplication
    store = parse_to_store(FIXTURE, tmp_path)
    window = MainWindow(store)
    qtbot.addWidget(window)

    tabs = window.tab_widget
    assert tabs.count() == 5
    assert "assignments" in tabs.tabText(0)
    assert "(4)" in tabs.tabText(3)  # properties has 4 rows


def test_model_reflects_vectorized_filter(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    store = parse_to_store(FIXTURE, tmp_path)
    model = ArrowTableModel(store.table("properties"))
    assert model.rowCount() == 4

    indices = filters.apply(model.table, [FilterClause("doc_kind", "equals", "B2")])
    model.set_view(indices)
    assert model.rowCount() == 1

    doc_number_col = model.columns.index("doc_number")
    cell = model.data(model.index(0, doc_number_col), Qt.ItemDataRole.DisplayRole)
    assert cell == "10987654"  # the single granted (B2) patent


def test_model_reset_view_restores_all_rows(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    store = parse_to_store(FIXTURE, tmp_path)
    model = ArrowTableModel(store.table("assignors"))
    model.set_view(filters.apply(model.table, [FilterClause("name", "contains", "smith")]))
    assert model.rowCount() == 1
    model.reset_view()
    assert model.rowCount() == 3


def test_row_selection_updates_status_bar(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    store = parse_to_store(FIXTURE, tmp_path)
    window = MainWindow(store)
    qtbot.addWidget(window)
    panel = window.current_panel()
    assert panel is not None
    panel._view.selectRow(0)
    status = window.statusBar()
    assert status is not None
    assert "selected" in status.currentMessage()
