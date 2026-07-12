"""Headless tests for the CPC UI: the two step editors and the CPC settings dialog."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pytestqt")

from uspto_assignments import CpcConfig, CpcMatchStep, FetchCpcStep
from uspto_assignments_ui.app import create_app
from uspto_assignments_ui.settings import CpcConfigStore
from uspto_assignments_ui.widgets.batch_dialog import (
    CpcMatchStepDialog,
    FetchCpcStepDialog,
)
from uspto_assignments_ui.widgets.cpc_settings_dialog import CpcSettingsDialog


def test_fetch_cpc_dialog_roundtrip(qtbot: Any) -> None:
    create_app([])
    original = FetchCpcStep(table="flat", column="doc_number", kind_column="doc_kind")
    dialog = FetchCpcStepDialog(original)
    qtbot.addWidget(dialog)
    step = dialog.step()
    assert step.column == "doc_number" and step.kind_column == "doc_kind"


def test_cpc_match_dialog_roundtrip(qtbot: Any) -> None:
    create_app([])
    original = CpcMatchStep(
        table="flat",
        portfolio_mode="footprint_file",
        portfolio_path="/tmp/pf.csv",
        buyer_column="assignee_names",
    )
    dialog = CpcMatchStepDialog(original)
    qtbot.addWidget(dialog)
    step = dialog.step()
    assert step.portfolio_mode == "footprint_file"
    assert step.portfolio_path == "/tmp/pf.csv"
    assert step.buyer_column == "assignee_names"


def test_cpc_settings_dialog_saves_config(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    store = CpcConfigStore(tmp_path / "cpc_config.json")
    dialog = CpcSettingsDialog(store)
    qtbot.addWidget(dialog)
    dialog._type.setCurrentIndex(dialog._type.findData("local_file"))
    dialog._path.setText(str(tmp_path / "cpc.tsv"))
    dialog._grain.setCurrentText("main_group")
    dialog._save()

    reloaded = store.load()
    assert reloaded.source.type == "local_file"
    assert reloaded.source.path == str(tmp_path / "cpc.tsv")
    assert reloaded.match.grain == "main_group"
    assert "api_key" not in _key_text(
        dialog
    )  # never captures the key itself, only the env-var name


def test_cpc_settings_never_stores_key(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    store = CpcConfigStore(tmp_path / "cpc_config.json")
    dialog = CpcSettingsDialog(store)
    qtbot.addWidget(dialog)
    dialog._save()
    text = (tmp_path / "cpc_config.json").read_text(encoding="utf-8")
    assert "api_key_env" in text  # the env-var NAME is stored
    config = CpcConfig()
    assert config.source.api_key_env in text


def _key_text(dialog: CpcSettingsDialog) -> str:
    return dialog._api_key_env.text()
