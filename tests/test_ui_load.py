"""Headless tests for Phase 6: landing tiles, load template, and pagination."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pytestqt")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QPushButton

from uspto_assignments import parse_to_store
from uspto_assignments_ui.app import create_app
from uspto_assignments_ui.widgets.landing import LandingPage
from uspto_assignments_ui.widgets.load_dialog import LoadDialog
from uspto_assignments_ui.widgets.table_panel import TablePanel

FIXTURE = Path(__file__).parent / "fixtures" / "sample_assignment.xml"


def test_landing_tile_emits_open_signal(qtbot: Any) -> None:
    create_app([])
    landing = LandingPage()
    qtbot.addWidget(landing)
    with qtbot.waitSignal(landing.open_file_requested, timeout=1000):
        landing.findChildren(QPushButton)[0].click()  # the "Open XML/ZIP" accent tile


def test_load_dialog_template_reports_choices(qtbot: Any) -> None:
    create_app([])
    dialog = LoadDialog()
    qtbot.addWidget(dialog)
    dialog._max.setValue(50)
    dialog._page.setCurrentIndex(0)  # 100 / page

    # uncheck the first column of the "properties" table
    tree = dialog._fields
    props_item = next(
        item
        for i in range(tree.topLevelItemCount())
        if (item := tree.topLevelItem(i)) is not None and item.text(0) == "properties"
    )
    first_col = props_item.child(0)
    assert first_col is not None
    dropped = first_col.text(0)
    first_col.setCheckState(0, Qt.CheckState.Unchecked)

    template = dialog.template()
    assert template.max_records == 50
    assert template.page_size == 100
    assert dropped not in template.columns["properties"]
    assert len(template.columns["properties"]) == 7  # 8 columns minus the one unchecked


def test_load_dialog_zero_records_means_all(qtbot: Any) -> None:
    create_app([])
    dialog = LoadDialog()
    qtbot.addWidget(dialog)
    assert dialog._max.value() == 0
    assert dialog.template().max_records is None


def test_table_panel_paginates(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    store = parse_to_store(FIXTURE, tmp_path / "store")
    panel = TablePanel(store.table("properties"), page_size=2)
    qtbot.addWidget(panel)

    assert panel._model.rowCount() == 2  # first page shows 2 of 4
    assert len(panel.current_view_rows()) == 4  # full filtered set is still all rows

    panel._pager._go(1)  # next page
    assert panel._model.rowCount() == 2  # second page: the remaining 2 rows


def test_table_panel_page_size_all_shows_everything(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    store = parse_to_store(FIXTURE, tmp_path / "store")
    panel = TablePanel(store.table("properties"), page_size=None)
    qtbot.addWidget(panel)
    assert panel._model.rowCount() == 4
