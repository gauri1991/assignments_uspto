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

import pyarrow as pa
import pyarrow.parquet as pq
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog

from uspto_assignments import filters, parse_to_store
from uspto_assignments.filters import FilterClause
from uspto_assignments_ui.app import create_app, load_stylesheet
from uspto_assignments_ui.main_window import MainWindow
from uspto_assignments_ui.models.arrow_table_model import ArrowTableModel
from uspto_assignments_ui.settings import RecentStore, UiStateStore
from uspto_assignments_ui.widgets.column_editor import ColumnEditorDialog
from uspto_assignments_ui.widgets.landing import LandingPage

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


def test_open_parquet_file_loads_single_table(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    table = pa.table({"patent": ["10000000", "11000000"], "codes": [["H04L", "G06F"], ["A61F"]]})
    path = tmp_path / "my_export.parquet"
    pq.write_table(table, str(path))  # pyright: ignore[reportUnknownMemberType]

    window = MainWindow()
    qtbot.addWidget(window)
    window._recent_store = RecentStore(tmp_path / "recent.json")
    window._ui_state = UiStateStore(tmp_path / "ui.json")
    window._open_table_file(path)

    assert window.tab_widget.count() == 1
    assert "my_export" in window.tab_widget.tabText(0)  # tab named after the file
    panel = window.current_panel()
    assert panel is not None
    assert panel._model.columns == ["patent", "codes"]
    assert panel.table.column("codes").to_pylist() == ["H04L; G06F", "A61F"]  # list joined
    assert window._recent_store.load()[0].kind == "table"  # recorded as a table file


def test_open_recent_routes_data_file_to_viewer(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    path = tmp_path / "d.parquet"
    pq.write_table(pa.table({"x": ["1"]}), str(path))  # pyright: ignore[reportUnknownMemberType]
    window = MainWindow()
    qtbot.addWidget(window)
    window._open_recent(str(path))  # a .parquet recent must open in the viewer, not the XML parser
    assert window.tab_widget.count() == 1
    assert window.current_panel() is not None


def test_landing_page_emits_open_table_requested(qtbot: Any) -> None:
    create_app([])
    landing = LandingPage()
    qtbot.addWidget(landing)
    with qtbot.waitSignal(landing.open_table_requested, timeout=1000):
        landing.open_table_requested.emit()


def test_edit_columns_drops_and_renames(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_app([])
    pq.write_table(  # pyright: ignore[reportUnknownMemberType]
        pa.table({"patent": ["10000000"], "codes": [["H04L"]], "junk": ["x"]}),
        str(tmp_path / "e.parquet"),
    )
    window = MainWindow()
    qtbot.addWidget(window)
    window._open_table_file(tmp_path / "e.parquet")

    # accept a plan: drop 'junk', rename 'patent' -> 'grant'
    def fake_exec(self: ColumnEditorDialog) -> int:
        junk, patent = self._grid.item(2, 0), self._grid.item(0, 1)
        assert junk is not None and patent is not None
        junk.setCheckState(Qt.CheckState.Unchecked)  # drop the junk column
        patent.setText("grant")  # rename patent -> grant
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ColumnEditorDialog, "exec", fake_exec)
    window._edit_columns()

    panel = window.current_panel()
    assert panel is not None
    assert panel._model.columns == ["grant", "codes"]  # junk dropped, patent renamed, order kept


def test_column_editor_plan_reorders(qtbot: Any) -> None:
    create_app([])
    dialog = ColumnEditorDialog(["a", "b", "c"])
    qtbot.addWidget(dialog)
    dialog._grid.setCurrentCell(2, 0)  # select 'c'
    dialog._move(-1)  # move it up -> a, c, b
    kept, renames = dialog.column_plan()
    assert kept == ["a", "c", "b"]
    assert renames == {}
