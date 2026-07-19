"""Headless regression tests: window/dialog lifecycle around background threads + temp stores."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pytestqt")

from PyQt6.QtCore import QThread

from uspto_assignments_ui.app import create_app
from uspto_assignments_ui.main_window import MainWindow
from uspto_assignments_ui.settings import EntityMemoryStore
from uspto_assignments_ui.widgets.entity_dialog import EntityDialog
from uspto_assignments_ui.widgets.load_dialog import LoadTemplate

FIXTURE = Path(__file__).parent / "fixtures" / "sample_assignment.xml"


def test_main_window_refuses_close_while_task_runs(qtbot: Any) -> None:
    """Regression: closing mid-parse/export destroyed a live QThread and aborted the process."""
    create_app([])
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window._thread = QThread(window)  # simulate an in-flight parse/export worker
    assert not window.close()
    assert window.isVisible()
    window._thread = None
    assert window.close()


def test_parse_temp_store_removed_when_dataset_closed(qtbot: Any) -> None:
    """Regression: every GUI parse leaked its temp Arrow store dir in /tmp forever."""
    create_app([])
    window = MainWindow()
    qtbot.addWidget(window)
    window._start_parse(FIXTURE, LoadTemplate())
    qtbot.waitUntil(lambda: window._thread is None, timeout=10_000)
    store_dir = window._store_dir
    assert store_dir is not None and store_dir.is_dir()
    window._close_dataset()
    assert not store_dir.exists()


def test_entity_clear_keeps_alias_review_filter_applied(qtbot: Any, tmp_path: Path) -> None:
    """Regression: Clear swapped in a fresh unfiltered model while the filter widgets stayed set."""
    create_app([])
    store = EntityMemoryStore(tmp_path / "entities.json", pointer=tmp_path / "ptr.json")
    dialog = EntityDialog(store)
    qtbot.addWidget(dialog)
    dialog._review_only.setChecked(True)
    dialog._review_cap.setValue(90)
    dialog._clear()
    dialog._memory.add_canonical("ACME CORP")
    dialog._memory.apply_learned([("acme corporation", "ACME CORP", 99)])
    dialog._alias_model.refresh()
    # The 99-score alias sits above the review cap, so the still-active filter must hide it.
    assert dialog._alias_model.rowCount() == 0
