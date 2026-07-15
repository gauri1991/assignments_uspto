"""Headless tests for the batch UI: template store, dialog build, and an end-to-end run."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pytestqt")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QListWidget, QMessageBox

from uspto_assignments import (
    AggregateStep,
    BatchEvent,
    BatchTemplate,
    ClassifyStep,
    CompareStep,
    DedupeStep,
    DeriveStep,
    EntityMemory,
    ExportStep,
    FilterClause,
    FilterStep,
    LoadConfig,
    NormalizeStep,
    ReferenceMatchStep,
    SelectStep,
    SortStep,
    TransferTypeStep,
    columns_after,
    dump_templates,
    load_templates,
    run_preview,
)
from uspto_assignments_ui.app import create_app
from uspto_assignments_ui.models import EntityAliasModel
from uspto_assignments_ui.settings import BatchTemplateStore, EntityMemoryStore, UiStateStore
from uspto_assignments_ui.widgets import batch_dialog as bd
from uspto_assignments_ui.widgets import entity_dialog as ed
from uspto_assignments_ui.widgets.batch_dialog import (
    _PRESETS,
    AggregateStepDialog,
    BatchDialog,
    ClassifyStepDialog,
    CompareStepDialog,
    DedupeStepDialog,
    DeriveStepDialog,
    ExportStepDialog,
    FilterStepDialog,
    NormalizeStepDialog,
    ReferenceMatchStepDialog,
    SelectStepDialog,
    SortStepDialog,
    TransferTypeStepDialog,
)
from uspto_assignments_ui.widgets.entity_dialog import EntityDialog
from uspto_assignments_ui.widgets.filter_bar import FilterBar
from uspto_assignments_ui.widgets.preview_dialog import PreviewDialog

FIXTURE = Path(__file__).parent / "fixtures" / "sample_assignment.xml"


def _template() -> BatchTemplate:
    return BatchTemplate(
        name="granted",
        load=LoadConfig(),
        steps=[
            FilterStep(table="properties", clauses=[FilterClause("doc_kind", "equals", "B2")]),
            ExportStep(fmt="parquet", tables=["properties"]),
        ],
    )


def test_batch_template_store_roundtrip(tmp_path: Path) -> None:
    store = BatchTemplateStore(tmp_path / "batch.json")
    store.add(_template())
    store.add(BatchTemplate(name="granted", load=LoadConfig(limit=5)))  # replace same name
    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].load.limit == 5
    store.delete("granted")
    assert store.load() == []


def test_batch_dialog_builds_template_from_ui(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    store = BatchTemplateStore(tmp_path / "batch.json")
    dialog = BatchDialog(store)
    qtbot.addWidget(dialog)
    dialog._template_name.setText("mine")
    dialog._max.setValue(10)
    dialog._steps = list(_template().steps)
    template = dialog.template()
    assert template.name == "mine"
    assert template.load.limit == 10
    assert isinstance(template.steps[0], FilterStep)
    assert isinstance(template.steps[1], ExportStep)


def test_add_folder_expands_xml_zip_recursively(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_app([])
    dialog = BatchDialog(BatchTemplateStore(tmp_path / "batch.json"))
    qtbot.addWidget(dialog)
    folder = tmp_path / "dumps"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.xml").write_text("<x/>", encoding="utf-8")
    (folder / "b.zip").write_text("", encoding="utf-8")
    (folder / "sub" / "c.XML").write_text("<x/>", encoding="utf-8")  # nested, mixed case
    (folder / "notes.txt").write_text("skip me", encoding="utf-8")  # ignored

    monkeypatch.setattr(
        bd.QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(folder))
    )
    dialog._add_folder()
    added = sorted(
        Path(dialog._inputs.item(i).text()).name  # type: ignore[union-attr]
        for i in range(dialog._inputs.count())
    )
    assert added == ["a.xml", "b.zip", "c.XML"]  # every .xml/.zip, recursive; the .txt is skipped


def test_add_folder_adds_parsed_dataset_folder_as_single_input(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_app([])
    dialog = BatchDialog(BatchTemplateStore(tmp_path / "batch.json"))
    qtbot.addWidget(dialog)
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "flat.parquet").write_text("", encoding="utf-8")  # looks like a parsed dataset

    monkeypatch.setattr(
        bd.QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(dataset))
    )
    dialog._add_folder()
    paths = [
        dialog._inputs.item(i).text()  # type: ignore[union-attr]
        for i in range(dialog._inputs.count())
    ]
    assert paths == [str(dataset)]  # the dataset folder itself is the single input


def test_add_folder_with_nothing_usable_adds_no_inputs(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_app([])
    dialog = BatchDialog(BatchTemplateStore(tmp_path / "batch.json"))
    qtbot.addWidget(dialog)
    empty = tmp_path / "empty"
    empty.mkdir()

    monkeypatch.setattr(
        bd.QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(empty))
    )
    monkeypatch.setattr(  # suppress the "nothing to add" popup
        bd.QMessageBox, "information", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    )
    dialog._add_folder()
    assert dialog._inputs.count() == 0


def test_batch_dialog_runs_and_writes_output(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    store = BatchTemplateStore(tmp_path / "batch.json")
    dialog = BatchDialog(store, EntityMemoryStore(tmp_path / "entities.json"))
    qtbot.addWidget(dialog)

    dialog._template_name.setText("granted")
    dialog._steps = list(_template().steps)
    dialog._inputs.addItem(str(FIXTURE))
    out = tmp_path / "out"
    dialog._out_dir.setText(str(out))
    dialog._run()

    qtbot.waitUntil(lambda: "Done:" in dialog._console.toPlainText(), timeout=15000)
    result = next((out / "granted").glob("run_*/sample_assignment/properties.parquet"))
    assert result.is_file()
    reopened = pq.read_table(result)  # pyright: ignore[reportUnknownMemberType]
    assert reopened.num_rows == 1
    # the console mirrored the per-step batch events
    assert "filter properties" in dialog._console.toPlainText()


def test_filter_step_dialog_prefills_for_edit(qtbot: Any) -> None:
    create_app([])
    original = FilterStep(
        table="assignors", clauses=[FilterClause("name", "contains", "ACME")], combine="or"
    )
    dialog = FilterStepDialog(original)
    qtbot.addWidget(dialog)
    step = dialog.step()
    assert step.table == "assignors"
    assert step.combine == "or"
    assert step.clauses[0].value == "ACME"


def test_normalize_step_dialog_builds_step(qtbot: Any) -> None:
    create_app([])
    dialog = NormalizeStepDialog()
    qtbot.addWidget(dialog)
    dialog._table.setCurrentText("assignees")
    dialog._column.setCurrentText("name")
    dialog._threshold.setValue(85)
    step = dialog.step()
    # target is stored blank (mirrors the derived name) so it always re-derives; resolve confirms it
    assert (step.table, step.column, step.target, step.threshold) == ("assignees", "name", "", 85)
    assert step.resolved_target() == "name_canonical"


def test_normalize_step_dialog_auto_fills_target_and_separator(qtbot: Any) -> None:
    create_app([])
    dialog = NormalizeStepDialog()
    qtbot.addWidget(dialog)
    dialog._table.setCurrentText("flat")
    dialog._column.setCurrentText("assignor_names")  # a concatenated multi-party column
    step = dialog.step()
    assert step.resolved_target() == "assignor_names_canonical"  # derived from column, no clobber
    assert step.separator == "; "  # auto-suggested for *_names columns


def test_normalize_step_dialog_repairs_legacy_step(qtbot: Any) -> None:
    create_app([])
    # a pre-fix saved step: generic target on a concatenated column, no separator
    legacy = NormalizeStep(table="flat", column="assignor_names", target="name_canonical")
    dialog = NormalizeStepDialog(legacy)
    qtbot.addWidget(dialog)
    repaired = dialog.step()
    # opening + re-saving derives the correct distinct column and the split separator
    assert repaired.resolved_target() == "assignor_names_canonical"
    assert repaired.separator == "; "


def test_dedupe_step_dialog_builds_step(qtbot: Any) -> None:
    create_app([])
    dialog = DedupeStepDialog()
    qtbot.addWidget(dialog)
    dialog._table.setCurrentText("assignees")
    item = dialog._columns.item(0)
    assert item is not None
    item.setCheckState(Qt.CheckState.Checked)
    step = dialog.step()
    assert isinstance(step, DedupeStep)
    assert step.table == "assignees" and step.subset == [item.text()]


def test_select_step_dialog_builds_step(qtbot: Any) -> None:
    create_app([])
    dialog = SelectStepDialog(SelectStep(table="assignees", columns=["name"]))
    qtbot.addWidget(dialog)
    step = dialog.step()
    assert step.table == "assignees" and step.columns == ["name"]


def test_sort_step_dialog_builds_step(qtbot: Any) -> None:
    create_app([])
    dialog = SortStepDialog()
    qtbot.addWidget(dialog)
    dialog._table.setCurrentText("assignees")
    dialog._column.setCurrentText("name")
    dialog._ascending.setChecked(False)
    step = dialog.step()
    assert (step.table, step.column, step.ascending) == ("assignees", "name", False)


def test_derive_step_dialog_auto_fills_target(qtbot: Any) -> None:
    create_app([])
    dialog = DeriveStepDialog()
    qtbot.addWidget(dialog)
    dialog._table.setCurrentText("assignments")
    dialog._source.setCurrentText("recorded_date")
    step = dialog.step()
    assert isinstance(step, DeriveStep)
    assert step.op == "year" and step.target == "recorded_date_year"


def test_aggregate_step_dialog_builds_step(qtbot: Any) -> None:
    create_app([])
    dialog = AggregateStepDialog()
    qtbot.addWidget(dialog)
    dialog._table.setCurrentText("assignees")
    item = dialog._columns.item(dialog._columns.count() - 1)
    assert item is not None
    item.setCheckState(Qt.CheckState.Checked)
    step = dialog.step()
    assert isinstance(step, AggregateStep)
    assert step.table == "assignees" and step.group_by == [item.text()]


def test_all_step_kinds_describe_in_list(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    dialog = BatchDialog(BatchTemplateStore(tmp_path / "b.json"))
    qtbot.addWidget(dialog)
    dialog._steps = [
        NormalizeStep(table="assignees", column="name"),
        DedupeStep(table="assignees", subset=["name"]),
        SelectStep(table="assignees", columns=["name"]),
        SortStep(table="assignees", column="name", ascending=False),
        DeriveStep(table="assignments", source="recorded_date", op="year"),
        AggregateStep(table="assignees", group_by=["name"]),
        ClassifyStep(table="flat", column="assignor_names"),
        CompareStep(table="flat", left="assignor_names", right="assignee_names"),
        TransferTypeStep(),
    ]
    dialog._refresh_steps_list()
    labels = [dialog._steps_list.item(i).text() for i in range(dialog._steps_list.count())]  # type: ignore[union-attr]
    # list rows are numbered ("N. …") and may carry a ⚠ badge, so match the description substring
    assert any("Normalize" in x and "name_canonical" in x for x in labels)
    for kind in ("Deduplicate", "Select", "Sort", "Derive", "Aggregate", "Classify", "Compare"):
        assert any(f"{kind} ·" in x for x in labels), kind
    assert any("Transfer type ·" in x for x in labels)
    assert all(x[0].isdigit() for x in labels)  # every row is numbered


def test_classify_step_dialog_builds_step(qtbot: Any) -> None:
    create_app([])
    dialog = ClassifyStepDialog()
    qtbot.addWidget(dialog)
    dialog._table.setCurrentText("flat")
    dialog._column.setCurrentText("assignor_names")
    step = dialog.step()
    assert isinstance(step, ClassifyStep)
    assert step.resolved_target() == "assignor_names_type"
    assert step.separator == "; "  # auto-suggested for *_names columns


def test_compare_step_dialog_builds_step(qtbot: Any) -> None:
    create_app([])
    dialog = CompareStepDialog()
    qtbot.addWidget(dialog)
    dialog._table.setCurrentText("flat")
    dialog._left.setCurrentText("assignor_names")
    dialog._right.setCurrentText("assignee_names")
    dialog._action.setCurrentIndex(dialog._action.findData("drop_matches"))
    step = dialog.step()
    assert (step.left, step.right, step.action) == (
        "assignor_names",
        "assignee_names",
        "drop_matches",
    )


def test_transfer_type_step_dialog_builds_step(qtbot: Any) -> None:
    create_app([])
    dialog = TransferTypeStepDialog()
    qtbot.addWidget(dialog)
    dialog._assignor_type.setCurrentText("individual")
    dialog._assignee_type.setCurrentText("company")
    step = dialog.step()
    assert isinstance(step, TransferTypeStep)
    assert (step.assignor_type, step.assignee_type) == ("individual", "company")
    assert step.assignor_column == "assignor_names"  # defaulted for the flat table


def test_reference_match_step_dialog_builds_step(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    ref = tmp_path / "ref.tsv"
    ref.write_text("disambig_assignee_organization\tassignee_id\nACME INC\tA1\n", encoding="utf-8")
    dialog = ReferenceMatchStepDialog()
    qtbot.addWidget(dialog)
    dialog._table.setCurrentText("flat")
    dialog._column.setCurrentText("assignor_names")
    dialog._reference.setText(str(ref))
    dialog._id_column.setText("assignee_id")
    dialog._action.setCurrentIndex(dialog._action.findData("keep_matched"))
    step = dialog.step()
    assert isinstance(step, ReferenceMatchStep)
    assert step.column == "assignor_names"
    assert step.reference_path == str(ref)
    assert step.id_column == "assignee_id"
    assert step.action == "keep_matched"
    assert step.resolved_target() == "assignor_names_disambiguated"


def test_reference_match_step_describes_in_list(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    dialog = BatchDialog(BatchTemplateStore(tmp_path / "b.json"))
    qtbot.addWidget(dialog)
    dialog._steps = [
        ReferenceMatchStep(table="flat", column="assignor_names", reference_path="/x/ref.tsv")
    ]
    dialog._refresh_steps_list()
    label = dialog._steps_list.item(0).text()  # type: ignore[union-attr]
    assert "Reference match ·" in label and "ref.tsv" in label


def test_step_dialog_sees_columns_from_earlier_steps(qtbot: Any) -> None:
    create_app([])
    # a Select dialog opened after a normalize step should offer the derived canonical column
    steps = [NormalizeStep(table="flat", column="assignor_names")]
    bd._available_ctx = columns_after(LoadConfig(), steps, 1)
    try:
        dialog = SelectStepDialog()
        qtbot.addWidget(dialog)
        dialog._table.setCurrentText("flat")  # rebuilds the column list via the schema context
        items = [dialog._columns.item(i).text() for i in range(dialog._columns.count())]  # type: ignore[union-attr]
    finally:
        bd._available_ctx = None
    assert "assignor_names_canonical" in items


def test_export_dialog_columns_order_and_rename(qtbot: Any) -> None:
    create_app([])
    bd._available_ctx = {"flat": ["a", "b", "c"]}
    try:
        dialog = ExportStepDialog()
        qtbot.addWidget(dialog)
        # keep only flat checked
        for i in range(dialog._tables.count()):
            item = dialog._tables.item(i)
            state = Qt.CheckState.Checked if item.text() == "flat" else Qt.CheckState.Unchecked  # type: ignore[union-attr]
            item.setCheckState(state)  # type: ignore[union-attr]
        dialog._customize.setChecked(True)
        dialog._editor._states["flat"] = [("a", True, "A"), ("b", True, "b"), ("c", False, "c")]
        dialog._editor._load("flat")
        step = dialog.step()
    finally:
        bd._available_ctx = None
    assert step.columns == {"flat": ["a", "b"]}  # 'c' excluded, order preserved
    assert step.renames == {"flat": {"a": "A"}}  # only the actual rename recorded


def test_batch_dialog_reorder_duplicate_toggle(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    dialog = BatchDialog(BatchTemplateStore(tmp_path / "b.json"))
    qtbot.addWidget(dialog)
    dialog._steps = [
        NormalizeStep(table="flat", column="assignor_names"),
        ExportStep(fmt="csv"),
    ]
    dialog._refresh_steps_list()
    dialog._steps_list.setCurrentRow(0)
    dialog._move_step(1)
    assert [type(s).__name__ for s in dialog._steps] == ["ExportStep", "NormalizeStep"]
    dialog._steps_list.setCurrentRow(0)
    dialog._duplicate_step()
    assert [type(s).__name__ for s in dialog._steps] == [
        "ExportStep",
        "ExportStep",
        "NormalizeStep",
    ]
    dialog._steps_list.setCurrentRow(0)
    dialog._toggle_step()
    assert dialog._steps[0].enabled is False
    assert "disabled" in dialog._steps_list.item(0).text()  # type: ignore[union-attr]


def test_normalize_step_dialog_scorer_roundtrips(qtbot: Any) -> None:
    create_app([])
    dialog = NormalizeStepDialog(
        NormalizeStep(table="assignees", column="name", scorer="token_set")
    )
    qtbot.addWidget(dialog)
    assert dialog.step().scorer == "token_set"


def test_entity_store_relocate_moves_memory(tmp_path: Path) -> None:
    pointer = tmp_path / "pointer.json"
    first = tmp_path / "a" / "entities.json"
    store = EntityMemoryStore(first, pointer=pointer)
    store.save(EntityMemory(canonicals=["ACME CORP"]))

    second = tmp_path / "b" / "entities.json"
    store.relocate(second)
    assert store.path == second
    assert "ACME CORP" in EntityMemory.load(second).canonicals  # content carried over
    # a fresh store reads the persisted pointer and reopens the relocated file
    reopened = EntityMemoryStore(pointer=pointer)
    assert reopened.path == second


def test_entity_dialog_clear_then_save_empties_memory(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    store = EntityMemoryStore(tmp_path / "entities.json", pointer=tmp_path / "ptr.json")
    store.save(EntityMemory(canonicals=["ACME CORP", "BETA INC"]))
    dialog = EntityDialog(store)
    qtbot.addWidget(dialog)
    dialog._clear()  # clears the working copy only
    assert store.load().counts()[0] == 2  # not persisted until Save
    dialog._save()
    assert store.load().counts()[0] == 0  # cleared and saved


def test_entity_dialog_edits_are_working_copy_until_save(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    store = EntityMemoryStore(tmp_path / "entities.json", pointer=tmp_path / "ptr.json")
    memory = EntityMemory(canonicals=["ACME CORP", "BETA INC"])
    memory.resolve("acme corp llc", threshold=85)  # learn an alias
    store.save(memory)

    dialog = EntityDialog(store)
    qtbot.addWidget(dialog)
    dialog._memory.rename_canonical("ACME CORP", "ACME CORPORATION")
    dialog._memory.merge_canonicals("BETA INC", "ACME CORPORATION")
    assert "ACME CORP" in store.load().canonicals  # working copy only — disk unchanged until Save

    dialog._save()
    saved = store.load()
    assert "ACME CORPORATION" in saved.canonicals
    assert "ACME CORP" not in saved.canonicals and "BETA INC" not in saved.canonicals
    assert saved.resolve("acme corp llc", threshold=85)[0] == "ACME CORPORATION"  # alias followed


def test_entity_alias_model_reassigns_canonical(qtbot: Any) -> None:
    create_app([])
    memory = EntityMemory(canonicals=["ACME CORPORATION"])
    memory.set_alias("ACME CORP", "ACME CORPORATION")
    model = EntityAliasModel(memory)
    assert model.rowCount() == 1
    index = model.index(0, 1)  # the canonical cell
    assert model.setData(index, "ACME INC", Qt.ItemDataRole.EditRole)
    assert memory.resolve("ACME CORP", threshold=100)[0] == "ACME INC"  # reassigned


def test_batch_dialog_double_click_edits_a_step(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    dialog = BatchDialog(BatchTemplateStore(tmp_path / "b.json"))
    qtbot.addWidget(dialog)
    dialog._steps = [ExportStep(fmt="parquet", tables=["properties"])]
    dialog._refresh_steps_list()
    item = dialog._steps_list.item(0)
    assert item is not None and "Export" in item.text()  # steps are listed and editable


def test_entity_store_roundtrip_and_dialog(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    store = EntityMemoryStore(tmp_path / "entities.json")
    memory = EntityMemory(canonicals=["ACME CORP"])
    store.save(memory)
    assert "ACME CORP" in store.load().canonicals
    dialog = EntityDialog(store)  # constructs and reads counts without error
    qtbot.addWidget(dialog)


def test_batch_dialog_normalize_run_persists_memory(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    memory_store = EntityMemoryStore(tmp_path / "entities.json")
    dialog = BatchDialog(BatchTemplateStore(tmp_path / "b.json"), memory_store)
    qtbot.addWidget(dialog)
    dialog._template_name.setText("norm")
    dialog._steps = [
        NormalizeStep(table="assignees", column="name", target="name_canonical", threshold=85),
        ExportStep(fmt="csv", tables=["assignees"]),
    ]
    dialog._inputs.addItem(str(FIXTURE))
    dialog._out_dir.setText(str(tmp_path / "out"))
    dialog._run()

    qtbot.waitUntil(lambda: "Done:" in dialog._console.toPlainText(), timeout=15000)
    assert (tmp_path / "entities.json").is_file()
    assert EntityMemory.load(tmp_path / "entities.json").counts()[0] >= 1
    assert "normalizing" in dialog._console.toPlainText()
    assert "Entity memory:" in dialog._console.toPlainText()


def test_preview_dialog_shows_tables_and_step_stats(qtbot: Any) -> None:
    create_app([])
    template = BatchTemplate(
        name="p",
        steps=[
            NormalizeStep(table="assignees", column="name"),
            ExportStep(fmt="parquet", tables=["assignees"]),
        ],
    )
    tables, stats = run_preview(template, FIXTURE, limit=25)
    dialog = PreviewDialog(tables, stats)
    qtbot.addWidget(dialog)
    summaries = dialog.findChildren(QListWidget)
    assert summaries and summaries[0].count() == len(stats)  # one summary row per step


def test_batch_dialog_template_duplicate_and_import_export(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    store = BatchTemplateStore(tmp_path / "b.json")
    dialog = BatchDialog(store)
    qtbot.addWidget(dialog)
    dialog._template_name.setText("mine")
    dialog._steps = [NormalizeStep(table="flat", column="assignor_names"), ExportStep(fmt="csv")]

    dialog._duplicate_template()
    assert any(t.name == "mine copy" for t in store.load())  # duplicated under a new name

    out = tmp_path / "exported.json"
    dump_templates([dialog.template()], out)  # the file _export_template writes
    dialog._steps = []  # wipe, then re-apply from the file (the _import_template path)
    dialog._apply_template(load_templates(out)[0])
    assert [type(s).__name__ for s in dialog._steps] == ["NormalizeStep", "ExportStep"]


def test_batch_dialog_preset_applies(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    dialog = BatchDialog(BatchTemplateStore(tmp_path / "b.json"))
    qtbot.addWidget(dialog)
    _name, factory = _PRESETS[0]  # "Firm-to-firm transfers"
    dialog._apply_template(factory())
    assert dialog._template_name.text() == "Firm-to-firm transfers"
    assert any(type(s).__name__ == "TransferTypeStep" for s in dialog._steps)


def test_step_list_shows_warning_badge(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    dialog = BatchDialog(BatchTemplateStore(tmp_path / "b.json"))
    qtbot.addWidget(dialog)
    # a filter on a column that doesn't exist yet -> validation warning -> ⚠ badge + tooltip
    dialog._steps = [FilterStep(table="flat", clauses=[FilterClause("nope_col", "contains", "x")])]
    dialog._refresh_steps_list()
    item = dialog._steps_list.item(0)
    assert item is not None
    assert "⚠" in item.text()
    assert "nope_col" in item.toolTip()


# -- run lifecycle: typed progress, cancel, close guard --------------------


def test_progress_driven_by_event_kind_not_message_text(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    dialog = BatchDialog(BatchTemplateStore(tmp_path / "b.json"))
    qtbot.addWidget(dialog)
    dialog._progress.setRange(0, 2)
    dialog._completed = 0

    dialog._on_event(BatchEvent("success", "no marker here", kind="file_done"))
    assert dialog._progress.value() == 1
    # regression: a plain message that merely *looks* like a completion line must not count
    dialog._on_event(BatchEvent("info", "✓ looks done but is only a message"))
    assert dialog._progress.value() == 1


def test_close_blocked_while_running_then_allowed(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_app([])
    dialog = BatchDialog(
        BatchTemplateStore(tmp_path / "b.json"), EntityMemoryStore(tmp_path / "entities.json")
    )
    qtbot.addWidget(dialog)
    dialog._template_name.setText("granted")
    dialog._steps = list(_template().steps)
    dialog._inputs.addItem(str(FIXTURE))
    dialog._out_dir.setText(str(tmp_path / "out"))
    dialog.show()
    dialog._run()
    assert dialog._thread is not None
    assert dialog._cancel_btn.isVisible()

    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
    )
    dialog.close()
    assert dialog.isVisible()  # close refused while the run is active

    qtbot.waitUntil(lambda: dialog._thread is None, timeout=15000)
    assert not dialog._cancel_btn.isVisible()
    assert dialog._run_btn.isEnabled() and dialog._preview_btn.isEnabled()
    dialog.close()
    assert not dialog.isVisible()


def test_close_during_run_cancels_and_closes_after(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_app([])
    dialog = BatchDialog(
        BatchTemplateStore(tmp_path / "b.json"), EntityMemoryStore(tmp_path / "entities.json")
    )
    qtbot.addWidget(dialog)
    dialog._template_name.setText("granted")
    dialog._steps = list(_template().steps)
    dialog._inputs.addItem(str(FIXTURE))
    dialog._out_dir.setText(str(tmp_path / "out"))
    dialog.show()
    dialog._run()

    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    dialog.reject()  # the Close button path
    assert dialog._close_after_run is True
    assert dialog.isVisible()  # still open until the thread stops

    qtbot.waitUntil(lambda: dialog._thread is None and not dialog.isVisible(), timeout=15000)


def test_cancel_slot_requests_worker_stop(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    dialog = BatchDialog(
        BatchTemplateStore(tmp_path / "b.json"), EntityMemoryStore(tmp_path / "entities.json")
    )
    qtbot.addWidget(dialog)
    dialog._template_name.setText("granted")
    dialog._steps = list(_template().steps)
    dialog._inputs.addItem(str(FIXTURE))
    dialog._out_dir.setText(str(tmp_path / "out"))
    dialog._run()

    dialog._cancel()
    assert not dialog._cancel_btn.isEnabled()
    assert "Cancelling" in dialog._console.toPlainText()
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=15000)
    # with a single tiny input the run may already be past the stop check; either way it ends clean
    assert "Done:" in dialog._console.toPlainText()


# -- export dialog state + destructive-action guards -----------------------


def test_export_dialog_keeps_edits_when_tables_toggled(qtbot: Any) -> None:
    create_app([])
    dialog = ExportStepDialog()
    qtbot.addWidget(dialog)
    dialog._customize.setChecked(True)
    current = dialog._editor._pick.currentText()
    source_item = dialog._editor._grid.item(0, 0)
    rename_item = dialog._editor._grid.item(0, 1)
    assert source_item is not None and rename_item is not None
    source_name = source_item.text()
    rename_item.setText("renamed_out")

    # unchecking an unrelated table re-seeds the editor; the in-progress rename must survive
    for i in range(dialog._tables.count()):
        item = dialog._tables.item(i)
        if item is not None and item.text() != current:
            item.setCheckState(Qt.CheckState.Unchecked)
            break
    step = dialog.step()
    assert step.renames is not None
    assert step.renames[current][source_name] == "renamed_out"


def test_export_dialog_disables_ok_when_no_tables(qtbot: Any) -> None:
    create_app([])
    dialog = ExportStepDialog()
    qtbot.addWidget(dialog)
    assert dialog._ok is not None and dialog._ok.isEnabled()
    for i in range(dialog._tables.count()):
        dialog._tables.item(i).setCheckState(Qt.CheckState.Unchecked)  # type: ignore[union-attr]
    assert not dialog._ok.isEnabled()
    assert not dialog._hint.isHidden()
    dialog._tables.item(0).setCheckState(Qt.CheckState.Checked)  # type: ignore[union-attr]
    assert dialog._ok.isEnabled()
    assert dialog._hint.isHidden()


def test_delete_template_confirms_and_uses_saved_selection(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_app([])
    store = BatchTemplateStore(tmp_path / "b.json")
    store.add(_template())  # saved as "granted"
    dialog = BatchDialog(store)
    qtbot.addWidget(dialog)
    dialog._saved.setCurrentIndex(1)  # select "granted" (index 0 is the placeholder)
    dialog._template_name.setText("half-typed new name")  # must NOT decide what gets deleted

    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
    )
    dialog._delete_template()
    assert [t.name for t in store.load()] == ["granted"]  # declined -> untouched

    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    dialog._delete_template()
    assert store.load() == []


def test_build_reference_failure_does_not_pollute_path(
    qtbot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_app([])
    dialog = ReferenceMatchStepDialog()
    qtbot.addWidget(dialog)
    dialog._reference.setText("/existing/ref.parquet")

    def _boom(*_a: Any, **_k: Any) -> int:
        raise ValueError("bad file")

    monkeypatch.setattr(
        bd.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: ("/tmp/src.tsv", ""))
    )
    monkeypatch.setattr(
        bd.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("/tmp/dst.parquet", ""))
    )
    monkeypatch.setattr(bd, "extract_distinct_reference", _boom)
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(str(a[2])))
    )
    dialog._build_reference()
    assert dialog._reference.text() == "/existing/ref.parquet"  # error never becomes the path
    assert warnings and "bad file" in warnings[0]


def test_alias_tab_notes_truncation(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    memory = EntityMemory(aliases={f"alias {i:05d}": f"Canon {i % 7}" for i in range(5100)})
    store = EntityMemoryStore(tmp_path / "entities.json")
    store.save(memory)
    dialog = EntityDialog(store)
    qtbot.addWidget(dialog)
    assert dialog._alias_model.truncated
    assert "refine the search" in dialog._alias_note.text()

    dialog._alias_search.setText("alias 00001")
    dialog._refresh_aliases()  # what the debounce timer fires
    assert not dialog._alias_model.truncated
    assert "match(es)" in dialog._alias_note.text()


def test_filter_step_dialog_rebuilds_bar_on_table_change(qtbot: Any) -> None:
    create_app([])
    dialog = FilterStepDialog()
    qtbot.addWidget(dialog)
    original_bar = dialog._filter_bar
    dialog._table.setCurrentText("assignees")  # must swap in a bar with that table's columns
    assert dialog._filter_bar is not original_bar
    columns = [
        dialog._filter_bar._column.itemText(i) for i in range(dialog._filter_bar._column.count())
    ]
    assert "name" in columns  # an assignees column
    # regression: the old bar is detached immediately (no ghost painting a duplicate builder row)
    assert original_bar.parent() is None
    live_bars = [b for b in dialog.findChildren(FilterBar) if b.parent() is dialog]
    assert live_bars == [dialog._filter_bar]  # exactly one bar attached to the dialog


def test_filter_step_dialog_hides_quick_search(qtbot: Any) -> None:
    create_app([])
    dialog = FilterStepDialog()
    qtbot.addWidget(dialog)
    # the quick-search box is meaningless in a filter step (only clauses + combine are saved)
    assert not dialog._filter_bar._search.isVisible()


def test_run_without_normalize_does_not_write_entity_store(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    memory_path = tmp_path / "entities.json"
    dialog = BatchDialog(BatchTemplateStore(tmp_path / "b.json"), EntityMemoryStore(memory_path))
    qtbot.addWidget(dialog)
    dialog._template_name.setText("granted")
    dialog._steps = list(_template().steps)  # filter + export only: nothing to learn
    dialog._inputs.addItem(str(FIXTURE))
    dialog._out_dir.setText(str(tmp_path / "out"))
    dialog._run()

    qtbot.waitUntil(lambda: "Done:" in dialog._console.toPlainText(), timeout=15000)
    assert not memory_path.exists()  # untouched memory must not rewrite the store
    assert "Entity memory:" not in dialog._console.toPlainText()


def test_entity_dialog_seeds_from_disambiguated_reference(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_app([])
    reference = tmp_path / "disambiguated.tsv"
    reference.write_text(
        "assignee_id\tdisambig_assignee_organization\n"
        "a1\tAcme Corporation\n"
        "a2\tGlobex LLC\n"
        "a3\tAcme Corporation\n"  # duplicate mention -> one canonical
        "a4\t\n",  # individual row (empty org) -> skipped
        encoding="utf-8",
    )
    store = EntityMemoryStore(tmp_path / "entities.json")
    dialog = EntityDialog(store)
    qtbot.addWidget(dialog)

    monkeypatch.setattr(
        ed.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(reference), ""))
    )
    monkeypatch.setattr(
        ed.QInputDialog,
        "getText",
        staticmethod(lambda *a, **k: ("disambig_assignee_organization", True)),
    )
    dialog._seed_from_reference()
    qtbot.waitUntil(lambda: dialog._thread is None, timeout=15000)

    assert sorted(dialog._memory.canonicals) == ["Acme Corporation", "Globex LLC"]
    assert "seeded 2 new from reference" in dialog._counts.text()
    assert not (tmp_path / "entities.json").exists()  # working copy: only Save persists

    dialog._save()  # Save -> entities.json now holds the seeded canonicals
    reloaded = EntityMemory.load(tmp_path / "entities.json")
    assert sorted(reloaded.canonicals) == ["Acme Corporation", "Globex LLC"]


# -- match confidence: dialog fields + alias review queue -------------------


def test_normalize_dialog_roundtrips_score_and_review(qtbot: Any) -> None:
    create_app([])
    step = NormalizeStep(
        table="flat", column="assignor_names", emit_score=True, review_threshold=95
    )
    dialog = NormalizeStepDialog(step)
    qtbot.addWidget(dialog)
    assert dialog._emit_score.isChecked()
    assert dialog._review.value() == 95
    rebuilt = dialog.step()
    assert rebuilt.emit_score is True and rebuilt.review_threshold == 95
    # defaults stay off
    fresh = NormalizeStepDialog()
    qtbot.addWidget(fresh)
    built = fresh.step()
    assert built.emit_score is False and built.review_threshold == 0


def test_reference_dialog_roundtrips_score_and_review(qtbot: Any) -> None:
    create_app([])
    step = ReferenceMatchStep(
        table="flat", column="assignor_names", emit_score=True, review_threshold=93
    )
    dialog = ReferenceMatchStepDialog(step)
    qtbot.addWidget(dialog)
    rebuilt = dialog.step()
    assert rebuilt.emit_score is True and rebuilt.review_threshold == 93


def test_alias_model_score_column_and_review_filter(qtbot: Any) -> None:
    create_app([])
    memory = EntityMemory(canonicals=["ACME CORPORATION"])
    memory.set_alias("ACME CORP", "ACME CORPORATION")  # curated -> 100
    memory.resolve("ACME CORPORATON", threshold=85)  # fuzzy-learned -> < 100
    model = EntityAliasModel(memory)
    assert model.columnCount() == 3
    assert model.rowCount() == 2
    scores = {
        model.data(model.index(r, 0)): int(model.data(model.index(r, 2)))
        for r in range(model.rowCount())
    }
    assert scores["ACME CORP"] == 100
    fuzzy_key = next(k for k in scores if k != "ACME CORP")
    assert scores[fuzzy_key] < 100

    model.set_review_filter(100)  # review queue: only the fuzzy-learned alias remains
    assert model.rowCount() == 1
    assert model.data(model.index(0, 0)) == fuzzy_key
    model.set_review_filter(None)
    assert model.rowCount() == 2


def test_entity_dialog_review_filter_narrows_aliases(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    memory = EntityMemory(canonicals=["ACME CORPORATION"])
    memory.set_alias("ACME CORP", "ACME CORPORATION")
    memory.resolve("ACME CORPORATON", threshold=85)
    store = EntityMemoryStore(tmp_path / "entities.json")
    store.save(memory)
    dialog = EntityDialog(store)
    qtbot.addWidget(dialog)
    assert dialog._alias_model.rowCount() == 2
    dialog._review_cap.setValue(98)  # the sample typo scores 97
    dialog._review_only.setChecked(True)  # -> only the fuzzy learn shows
    assert dialog._alias_model.rowCount() == 1
    assert "match(es)" in dialog._alias_note.text()
    dialog._review_only.setChecked(False)
    assert dialog._alias_model.rowCount() == 2


def test_compare_dialog_roundtrips_score_and_review(qtbot: Any) -> None:
    create_app([])
    step = CompareStep(
        table="flat",
        left="assignor_names",
        right="assignee_names",
        method="fuzzy",
        emit_score=True,
        review_threshold=92,
    )
    dialog = CompareStepDialog(step)
    qtbot.addWidget(dialog)
    rebuilt = dialog.step()
    assert rebuilt.emit_score is True and rebuilt.review_threshold == 92


def test_step_descriptions_show_confidence_markers(qtbot: Any) -> None:
    create_app([])
    described = bd._describe_step(
        NormalizeStep(table="flat", column="assignor_names", emit_score=True, review_threshold=95)
    )
    assert "· score" in described and "review<95" in described
    plain = bd._describe_step(NormalizeStep(table="flat", column="assignor_names"))
    assert "score" not in plain and "review" not in plain


def test_mark_reviewed_confirms_alias_and_clears_review_queue(qtbot: Any, tmp_path: Path) -> None:
    create_app([])
    memory = EntityMemory(canonicals=["ACME CORPORATION"])
    memory.resolve("ACME CORPORATON", threshold=85)  # fuzzy learn < 100
    store = EntityMemoryStore(tmp_path / "entities.json")
    store.save(memory)
    dialog = EntityDialog(store)
    qtbot.addWidget(dialog)
    dialog._review_cap.setValue(100)
    dialog._review_only.setChecked(True)
    assert dialog._alias_model.rowCount() == 1  # the marginal alias is queued

    dialog._alias_table.selectRow(0)
    dialog._confirm_alias()
    assert dialog._alias_model.rowCount() == 0  # confirmed -> off the queue
    dialog._review_only.setChecked(False)
    scores = [
        int(dialog._alias_model.data(dialog._alias_model.index(r, 2)))
        for r in range(dialog._alias_model.rowCount())
    ]
    assert scores and all(s == 100 for s in scores)  # human-confirmed


def test_batch_dialog_output_defaults_to_data_out(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    create_app([])
    dialog = BatchDialog(
        BatchTemplateStore(tmp_path / "b.json"), ui_state=UiStateStore(tmp_path / "ui.json")
    )
    qtbot.addWidget(dialog)
    assert dialog._out_dir.text() == str(tmp_path / "data" / "out")


def test_batch_dialog_output_prefers_saved_dir(qtbot: Any, tmp_path: Path) -> None:
    state = UiStateStore(tmp_path / "ui.json")
    saved = tmp_path / "my_outputs"
    saved.mkdir()
    state.set_last_dir("output", str(saved))
    create_app([])
    dialog = BatchDialog(BatchTemplateStore(tmp_path / "b.json"), ui_state=state)
    qtbot.addWidget(dialog)
    assert dialog._out_dir.text() == str(saved)
