"""The batch-processing dialog: build/run/save pipeline templates with a live console."""

from __future__ import annotations

import copy
import html
import logging
import re
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, Qt, QThread
from PyQt6.QtGui import QAction, QCloseEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from uspto_assignments import (
    LEGACY_NORMALIZE_TARGET,
    PREVIEW_LIMIT,
    STORE_TABLES,
    TABLE_FILE_SUFFIXES,
    AggregateStep,
    AttachCpcFileStep,
    BatchEvent,
    BatchResult,
    BatchStep,
    BatchTemplate,
    ClassifyStep,
    CompareStep,
    CpcMatchStep,
    CpcRunContext,
    DedupeStep,
    DeriveStep,
    EntityMemory,
    ExportFormat,
    ExportStep,
    FetchCpcStep,
    FilterStep,
    LoadConfig,
    NormalizeStep,
    ReferenceMatchStep,
    SelectStep,
    SortStep,
    TemplateFormatError,
    TransferTypeStep,
    columns_after,
    columns_for,
    dump_templates,
    extract_distinct_reference,
    inputs_schema_base,
    is_dataset_dir,
    load_templates,
    match_input_files,
    probablepeople_available,
    reference_columns,
    run_preview,
    scorer_names,
    validate_template,
)
from uspto_assignments import (
    describe_step as _describe_step,
)

from ..help_content import step_note_text
from ..settings import BatchTemplateStore, CpcConfigStore, EntityMemoryStore, UiStateStore
from ..workers import BatchWorker, CallWorker, LogEmitter, QtLogHandler
from .cpc_settings_dialog import CpcSettingsDialog
from .field_tree import FieldTree
from .filter_bar import FilterBar
from .help_panel import HelpPanel
from .page import SectionLabel
from .preview_dialog import PreviewDialog

_PREVIEW_LIMIT = PREVIEW_LIMIT  # single source of truth: uspto_assignments.batch
_HELP_PANEL_WIDTH = 340  # widened/narrowed on the dialog when the Help panel opens/closes

_MAX_RECORDS_CAP = 100_000_000
_FORMATS: list[tuple[str, ExportFormat]] = [
    ("Parquet", "parquet"),
    ("CSV", "csv"),
    ("Excel (.xlsx)", "xlsx"),
    ("JSON", "json"),
    ("Feather (Arrow)", "feather"),
]
_CORE_LOGGER = "uspto_assignments"
# Batch inputs a folder scan / pattern pick will accept: raw XML/ZIP to parse, plus already-
# processed data files (parquet/arrow/feather/csv) loaded directly as the ``flat`` table.
_INPUT_SUFFIXES = (".xml", ".zip", *TABLE_FILE_SUFFIXES)
_DERIVE_OPS: list[tuple[str, str]] = [
    ("Year (YYYY of a date)", "year"),
    ("Month (MM of a date)", "month"),
    ("First part (split on '; ')", "split_first"),
    ("Uppercase", "upper"),
    ("Lowercase", "lower"),
]


def _classify_methods() -> list[tuple[str, str]]:
    """Classify method options; the probablepeople label reflects whether it is installed."""
    pp_label = (
        "ML (probablepeople)"
        if probablepeople_available()
        else "ML (probablepeople — not installed)"
    )
    return [
        ("Rules (fast, no dependency)", "rules"),
        ("ML (built-in, no setup)", "model"),
        (pp_label, "probablepeople"),
    ]


_COMBINE_MODES: list[tuple[str, str]] = [
    ("All parties agree", "all"),
    ("Any party", "any"),
    ("First party", "first"),
    ("Majority", "majority"),
]
_ENTITY_TYPES: list[str] = ["company", "individual", "unknown"]
_COMPARE_METHODS: list[tuple[str, str]] = [
    ("Exact (fast; ideal on canonical columns)", "exact"),
    ("Fuzzy (rapidfuzz ≥ threshold)", "fuzzy"),
]
_COMPARE_ACTIONS: list[tuple[str, str]] = [
    ("Flag matches (add true/false column)", "flag"),
    ("Drop matching rows", "drop_matches"),
    ("Keep only matching rows", "keep_matches"),
]
_REFERENCE_MODES: list[tuple[str, str]] = [
    ("Any party matches", "any"),
    ("All parties match", "all"),
]
_REFERENCE_ACTIONS: list[tuple[str, str]] = [
    ("Flag + normalize (add columns)", "flag"),
    ("Keep only matched (known companies)", "keep_matched"),
    ("Drop matched rows", "drop_matched"),
]
_REFERENCE_FILTER = "Reference (*.tsv *.csv *.parquet);;All files (*)"


def _preset_firm_to_firm() -> BatchTemplate:
    return BatchTemplate(
        name="Firm-to-firm transfers",
        steps=[TransferTypeStep(), ExportStep(fmt="parquet", tables=["flat"])],
    )


def _preset_top_assignees() -> BatchTemplate:
    return BatchTemplate(
        name="Top assignees",
        steps=[
            NormalizeStep(table="assignees", column="name"),
            AggregateStep(table="assignees", group_by=["name_canonical"]),
            ExportStep(fmt="csv", tables=["assignees_by_name_canonical"]),
        ],
    )


def _preset_enrich_flat() -> BatchTemplate:
    return BatchTemplate(
        name="Enrich flat (names + types)",
        steps=[
            NormalizeStep(table="flat", column="assignor_names"),
            NormalizeStep(table="flat", column="assignee_names"),
            ClassifyStep(table="flat", column="assignor_names"),
            ClassifyStep(table="flat", column="assignee_names"),
            ExportStep(fmt="parquet", tables=["flat"]),
        ],
    )


# Built-in example templates offered by "New from example ▾".
_PRESETS: list[tuple[str, Callable[[], BatchTemplate]]] = [
    ("Firm-to-firm transfers", _preset_firm_to_firm),
    ("Top assignees", _preset_top_assignees),
    ("Enrich flat (names + types)", _preset_enrich_flat),
]


# Columns available at the step being edited (base schema + columns added by earlier steps).
# BatchDialog sets this around each modal step dialog (dialogs are modal + single-threaded, so a
# module-scoped context is safe) and clears it afterwards; ``_cols`` reads it.
_available_ctx: dict[str, list[str]] | None = None


def _cols(table: str) -> list[str]:
    """Columns for ``table`` — schema-aware (earlier steps' columns) when a context is set."""
    if _available_ctx is not None and table in _available_ctx:
        return list(_available_ctx[table])
    try:
        return columns_for(table)
    except KeyError:  # a derived table (aggregate/cpc_match output) outside the context
        return []


def _checkable_columns(columns: list[str], checked: set[str] | None) -> QListWidget:
    """A checkable column list; ``checked=None`` checks every column."""
    widget = QListWidget()
    widget.setProperty("panel", "true")
    for name in columns:
        item = QListWidgetItem(name)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        on = checked is None or name in checked
        item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        widget.addItem(item)
    return widget


def _checked_columns(widget: QListWidget) -> list[str]:
    """Return the text of every checked item, preserving list order."""
    result: list[str] = []
    for i in range(widget.count()):
        item = widget.item(i)
        if item is not None and item.checkState() == Qt.CheckState.Checked:
            result.append(item.text())
    return result


def _set_checks(widget: QListWidget, names: list[str]) -> None:
    """Check exactly the items whose text is in ``names`` (uncheck the rest)."""
    wanted = set(names)
    for i in range(widget.count()):
        item = widget.item(i)
        if item is not None:
            on = item.text() in wanted
            item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)


class FilterStepDialog(QDialog):
    """Configure a filter step: a table plus filter clauses (AND/OR)."""

    def __init__(self, step: FilterStep | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Filter step")
        self.setMinimumWidth(720)  # the filter-builder row needs the room, or fields clip
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(SectionLabel("Filter step"))

        self._table = QComboBox()
        self._table.addItems(list(STORE_TABLES))
        self._table.currentIndexChanged.connect(self._rebuild_filter_bar)
        form = QFormLayout()
        form.addRow("Table", self._table)
        layout.addLayout(form)

        # Kept a direct child of the top-level VBox: _rebuild_filter_bar swaps it in place there.
        # The quick-search box is hidden — a filter STEP stores only clauses + combine, so a
        # free-text search here would be silently discarded.
        self._filter_bar = FilterBar(_cols(self._table.currentText()), show_quick_search=False)
        layout.addWidget(self._filter_bar)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if step is not None:
            self._table.setCurrentText(step.table)  # rebuilds the filter bar for that table
            self._filter_bar.set_state(step.clauses, step.combine, None)
        # ``columns`` (projection) and ``sort`` are spec-supported but have no widgets here —
        # carry them through so editing the clauses doesn't silently strip them from the JSON.
        self._carry_columns = step.columns if step is not None else None
        self._carry_sort = step.sort if step is not None else None

    def _rebuild_filter_bar(self) -> None:
        new_bar = FilterBar(_cols(self._table.currentText()), show_quick_search=False)
        old = self._filter_bar
        layout = self.layout()
        if layout is not None:
            layout.replaceWidget(old, new_bar)
        self._filter_bar = new_bar
        # replaceWidget only drops ``old`` from the layout; detach it now so it can't linger as a
        # painted ghost (deleteLater is deferred until the event loop spins).
        old.setParent(None)
        old.deleteLater()

    def step(self) -> FilterStep:
        """Return the configured filter step."""
        return FilterStep(
            table=self._table.currentText(),
            clauses=self._filter_bar.clauses(),
            combine=self._filter_bar.combine(),
            columns=self._carry_columns,
            sort=self._carry_sort,
        )


class _ExportColumnEditor(QWidget):
    """Per-table final-column editor: include (checkbox), reorder (up/down), and rename output."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self._pick = QComboBox()
        self._pick.currentIndexChanged.connect(self._on_table_changed)
        layout.addWidget(self._pick)
        self._grid = QTableWidget(0, 2)
        self._grid.setHorizontalHeaderLabels(["Column (check = include)", "Output name"])
        self._grid.setProperty("panel", "true")
        self._grid.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        vh = self._grid.verticalHeader()
        if vh is not None:
            vh.setVisible(False)
        hh = self._grid.horizontalHeader()
        if hh is not None:
            hh.setStretchLastSection(True)
        layout.addWidget(self._grid)
        row = QHBoxLayout()
        up = QPushButton("Move up")
        up.clicked.connect(lambda: self._move(-1))
        down = QPushButton("Move down")
        down.clicked.connect(lambda: self._move(1))
        row.addWidget(up)
        row.addWidget(down)
        row.addStretch(1)
        layout.addLayout(row)
        self._states: dict[str, list[tuple[str, bool, str]]] = {}
        self._current: str | None = None

    def set_tables(
        self,
        tables: list[str],
        columns: dict[str, list[str]] | None,
        renames: dict[str, dict[str, str]] | None,
    ) -> None:
        """Seed per-table column state from ``columns``/``renames`` (or all-included defaults)."""
        self._states = {}
        for table in tables:
            source_cols = _cols(table)
            chosen = (columns or {}).get(table)
            table_renames = (renames or {}).get(table, {})
            if chosen:  # explicit: included in the chosen order, then the rest unchecked
                ordered = [c for c in chosen if c in source_cols]
                rest = [c for c in source_cols if c not in ordered]
                self._states[table] = [(c, True, table_renames.get(c, c)) for c in ordered] + [
                    (c, False, table_renames.get(c, c)) for c in rest
                ]
            else:
                self._states[table] = [(c, True, table_renames.get(c, c)) for c in source_cols]
        self._pick.blockSignals(True)
        self._pick.clear()
        self._pick.addItems(tables)
        self._pick.blockSignals(False)
        self._current = tables[0] if tables else None
        self._load(self._current)

    def _on_table_changed(self) -> None:
        self._save()
        self._current = self._pick.currentText() or None
        self._load(self._current)

    def _load(self, table: str | None) -> None:
        self._grid.setRowCount(0)
        if not table:
            return
        for source, included, output in self._states.get(table, []):
            index = self._grid.rowCount()
            self._grid.insertRow(index)
            item = QTableWidgetItem(source)
            item.setFlags(
                (item.flags() | Qt.ItemFlag.ItemIsUserCheckable) & ~Qt.ItemFlag.ItemIsEditable
            )
            item.setCheckState(Qt.CheckState.Checked if included else Qt.CheckState.Unchecked)
            self._grid.setItem(index, 0, item)
            self._grid.setItem(index, 1, QTableWidgetItem(output))

    def _save(self) -> None:
        if not self._current:
            return
        rows: list[tuple[str, bool, str]] = []
        for index in range(self._grid.rowCount()):
            source_item = self._grid.item(index, 0)
            output_item = self._grid.item(index, 1)
            if source_item is None:
                continue
            source = source_item.text()
            included = source_item.checkState() == Qt.CheckState.Checked
            output = (output_item.text().strip() if output_item else "") or source
            rows.append((source, included, output))
        self._states[self._current] = rows

    def _move(self, delta: int) -> None:
        row = self._grid.currentRow()
        target = row + delta
        if row < 0 or target < 0 or target >= self._grid.rowCount():
            return
        for column in range(2):
            a = self._grid.takeItem(row, column)
            b = self._grid.takeItem(target, column)
            self._grid.setItem(row, column, b)
            self._grid.setItem(target, column, a)
        self._grid.setCurrentCell(target, 0)

    def result(
        self, tables: list[str]
    ) -> tuple[dict[str, list[str]] | None, dict[str, dict[str, str]] | None]:
        """Return ``(columns, renames)``; a table is omitted when it keeps all columns as-is."""
        self._save()
        columns: dict[str, list[str]] = {}
        renames: dict[str, dict[str, str]] = {}
        for table in tables:
            rows = self._states.get(table)
            if not rows:
                continue
            included = [(s, o) for (s, inc, o) in rows if inc]
            is_default = len(included) == len(rows) and all(s == o for s, _, o in rows)
            if is_default:
                continue  # keep all columns, no rename → leave as default (None)
            columns[table] = [s for s, _ in included]
            table_renames = {s: o for s, o in included if o != s}
            if table_renames:
                renames[table] = table_renames
        return (columns or None), (renames or None)


class ExportStepDialog(QDialog):
    """Configure an export step: format, which tables, and the final columns (order + rename)."""

    def __init__(self, step: ExportStep | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export step")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(SectionLabel("Export step"))

        self._format = QComboBox()
        for label, fmt in _FORMATS:
            self._format.addItem(label, fmt)
        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("Format"))
        fmt_row.addWidget(self._format, 1)
        layout.addLayout(fmt_row)

        layout.addWidget(QLabel("Tables (all checked = every table)"))
        self._tables = QListWidget()
        chosen = None if step is None else step.tables
        # Offer every table available at this pipeline position — including tables earlier steps
        # create (aggregate / cpc_match outputs) — plus any the step already names, so editing an
        # export never silently drops a non-store table.
        known = list(_available_ctx) if _available_ctx is not None else list(STORE_TABLES)
        self._table_names: list[str] = known + [t for t in (chosen or []) if t not in known]
        for name in self._table_names:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = chosen is None or name in chosen
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self._tables.addItem(item)
        self._tables.itemChanged.connect(self._refresh_editor)
        layout.addWidget(self._tables, 1)

        self._customize = QCheckBox("Choose final columns (order + rename)")
        self._customize.toggled.connect(self._on_customize_toggled)
        layout.addWidget(self._customize)
        self._editor = _ExportColumnEditor()
        self._editor.setVisible(False)
        # Weighted heavier than the tables list so toggling the editor redistributes space
        # instead of erratically resizing the dialog.
        layout.addWidget(self._editor, 2)

        self._hint = QLabel("Check at least one table to export.")
        self._hint.setVisible(False)
        layout.addWidget(self._hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._pending_columns = None if step is None else step.columns
        self._pending_renames = None if step is None else step.renames
        self._editor_tables: list[str] = []  # tables currently loaded in the column editor
        if step is not None:
            index = self._format.findData(step.fmt)
            if index >= 0:
                self._format.setCurrentIndex(index)
            if step.columns or step.renames:
                self._customize.setChecked(True)
        self._refresh_editor()  # prime the OK/hint state (a legacy step may have tables=[])

    def _checked_tables(self) -> list[str]:
        checked: list[str] = []
        for i in range(self._tables.count()):
            item = self._tables.item(i)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                checked.append(item.text())
        return checked

    def _on_customize_toggled(self, on: bool) -> None:
        self._editor.setVisible(on)
        if on:
            self._refresh_editor()

    def _refresh_editor(self) -> None:
        none_checked = not self._checked_tables()
        if self._ok is not None:
            self._ok.setEnabled(not none_checked)  # exporting nothing is a mistake, not a request
        self._hint.setVisible(none_checked)
        if not self._customize.isChecked():
            return
        if self._editor_tables:  # harvest in-progress edits before re-seeding the grid
            self._pending_columns, self._pending_renames = self._editor.result(self._editor_tables)
        tables = self._checked_tables() or list(self._table_names)
        self._editor.set_tables(tables, self._pending_columns, self._pending_renames)
        self._editor_tables = tables

    def step(self) -> ExportStep:
        """Return the configured export step (``tables=None`` when all are selected)."""
        checked = self._checked_tables()
        tables = None if len(checked) == len(self._table_names) else checked
        columns: dict[str, list[str]] | None = None
        renames: dict[str, dict[str, str]] | None = None
        if self._customize.isChecked():
            columns, renames = self._editor.result(checked or list(self._table_names))
        return ExportStep(
            fmt=self._format.currentData(), tables=tables, columns=columns, renames=renames
        )


class NormalizeStepDialog(QDialog):
    """Configure a normalize step: fuzzy-map a name column to a canonical column."""

    def __init__(  # noqa: PLR0915 - linear widget assembly
        self, step: NormalizeStep | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Normalize step")
        self.setMinimumWidth(560)  # wide enough for the long "Learn new canonicals…" checkbox label
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(SectionLabel("Normalize step"))

        form = QFormLayout()
        self._table = QComboBox()
        self._table.addItems(list(STORE_TABLES))
        self._column = QComboBox()
        self._target = QLineEdit()
        self._target.setPlaceholderText("(auto: <column>_canonical)")
        self._separator = QLineEdit()
        self._separator.setPlaceholderText('e.g. "; " to normalize each concatenated part')
        self._threshold = QSpinBox()
        self._threshold.setRange(0, 100)
        self._threshold.setValue(90)
        self._scorer = QComboBox()
        self._scorer.addItems(scorer_names())
        self._learn = QCheckBox("Learn new canonicals (uncheck to match a curated memory only)")
        self._learn.setChecked(True)
        self._emit_score = QCheckBox("Add match-score column (confidence 0–100)")
        self._emit_type = QCheckBox("Add entity-type column (from the memory's stored tags)")
        self._review = QSpinBox()
        self._review.setRange(0, 100)
        self._review.setSpecialValueText("Off")  # 0 = no review flagging
        form.addRow("Table", self._table)
        form.addRow("Name column", self._column)
        form.addRow("Canonical column", self._target)
        form.addRow("Split separator", self._separator)
        form.addRow("Match threshold", self._threshold)
        form.addRow("Scorer", self._scorer)
        form.addRow("", self._learn)  # aligned under the field column
        form.addRow("", self._emit_score)
        form.addRow("", self._emit_type)
        form.addRow("Flag review below", self._review)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._table.currentIndexChanged.connect(self._rebuild_columns)
        self._column.currentTextChanged.connect(self._suggest_for_column)
        self._rebuild_columns()  # fills columns + primes target/separator suggestions

        if step is not None:
            self._table.setCurrentText(step.table)
            # setCurrentText fires _suggest_for_column, priming a derived target + separator.
            self._column.setCurrentText(step.column)
            # Only override the suggestions with genuine custom values; a legacy/blank target or
            # blank separator keeps the (repaired) auto-derived ones, fixing pre-fix templates.
            if step.target and step.target != LEGACY_NORMALIZE_TARGET:
                self._target.setText(step.target)
            if step.separator:
                self._separator.setText(step.separator)
            self._threshold.setValue(step.threshold)
            self._scorer.setCurrentText(step.scorer)
            self._learn.setChecked(step.learn)
            self._emit_score.setChecked(step.emit_score)
            self._emit_type.setChecked(step.emit_type)
            self._review.setValue(step.review_threshold)

    def _rebuild_columns(self) -> None:
        self._column.clear()
        self._column.addItems(_cols(self._table.currentText()))
        name_index = self._column.findText("name")
        if name_index >= 0:
            self._column.setCurrentIndex(name_index)

    def _suggest_for_column(self, column: str) -> None:
        """Auto-fill the canonical target and suggest '; ' for concatenated ``*_names`` columns."""
        if column:
            self._target.setText(f"{column}_canonical")
            self._separator.setText("; " if column.endswith("_names") else "")

    def step(self) -> NormalizeStep:
        """Return the configured normalize step (blank target derives ``<column>_canonical``)."""
        column = self._column.currentText()
        target = self._target.text().strip()
        # Store blank when it just mirrors the derived name, so saved templates always re-derive.
        stored_target = "" if target == f"{column}_canonical" else target
        return NormalizeStep(
            table=self._table.currentText(),
            column=column,
            target=stored_target,
            threshold=self._threshold.value(),
            separator=self._separator.text(),
            learn=self._learn.isChecked(),
            scorer=self._scorer.currentText(),
            emit_score=self._emit_score.isChecked(),
            emit_type=self._emit_type.isChecked(),
            review_threshold=self._review.value(),
        )


class DedupeStepDialog(QDialog):
    """Configure a dedupe step: drop duplicate rows (keep first), keyed by chosen columns."""

    def __init__(self, step: DedupeStep | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Deduplicate step")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(SectionLabel("Deduplicate step"))

        self._table = QComboBox()
        self._table.addItems(list(STORE_TABLES))
        form = QFormLayout()
        form.addRow("Table", self._table)
        layout.addLayout(form)

        layout.addWidget(QLabel("Key columns (none checked = whole row)"))
        # Default: no key columns checked (dedupe on the whole row). Kept a direct child of the
        # top-level VBox: _rebuild_columns swaps it in place there.
        self._columns = _checkable_columns(_cols(self._table.currentText()), set())
        layout.addWidget(self._columns)
        self._table.currentIndexChanged.connect(self._rebuild_columns)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if step is not None:
            self._table.setCurrentText(step.table)  # may rebuild the column list
            _set_checks(self._columns, step.subset or [])

    def _rebuild_columns(self) -> None:
        new = _checkable_columns(_cols(self._table.currentText()), set())
        old = self._columns
        layout = self.layout()
        if layout is not None:
            layout.replaceWidget(old, new)
        self._columns = new
        old.setParent(None)  # detach now so the old list can't paint as a ghost
        old.deleteLater()

    def step(self) -> DedupeStep:
        """Return the configured dedupe step (``subset=None`` when no key columns are checked)."""
        chosen = _checked_columns(self._columns)
        return DedupeStep(table=self._table.currentText(), subset=chosen or None)


class SelectStepDialog(QDialog):
    """Configure a select step: keep (and reorder) a chosen set of columns."""

    def __init__(self, step: SelectStep | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select columns step")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(SectionLabel("Select columns step"))

        self._table = QComboBox()
        self._table.addItems(list(STORE_TABLES))
        form = QFormLayout()
        form.addRow("Table", self._table)
        layout.addLayout(form)

        layout.addWidget(QLabel("Columns to keep"))
        # Default: keep all columns (checked); a new table resets to all-kept. Kept a direct
        # child of the top-level VBox: _rebuild_columns swaps it in place there.
        self._columns = _checkable_columns(_cols(self._table.currentText()), None)
        layout.addWidget(self._columns)
        self._table.currentIndexChanged.connect(self._rebuild_columns)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if step is not None:
            self._table.setCurrentText(step.table)  # may rebuild the column list
            _set_checks(self._columns, step.columns)

    def _rebuild_columns(self) -> None:
        new = _checkable_columns(_cols(self._table.currentText()), None)
        old = self._columns
        layout = self.layout()
        if layout is not None:
            layout.replaceWidget(old, new)
        self._columns = new
        old.setParent(None)  # detach now so the old list can't paint as a ghost
        old.deleteLater()

    def step(self) -> SelectStep:
        """Return the configured select step."""
        return SelectStep(table=self._table.currentText(), columns=_checked_columns(self._columns))


class SortStepDialog(QDialog):
    """Configure a sort step: order a table by one column."""

    def __init__(self, step: SortStep | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sort step")
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(SectionLabel("Sort step"))

        form = QFormLayout()
        self._table = QComboBox()
        self._table.addItems(list(STORE_TABLES))
        self._column = QComboBox()
        self._ascending = QCheckBox("Ascending")
        self._ascending.setChecked(True)
        form.addRow("Table", self._table)
        form.addRow("Column", self._column)
        form.addRow("", self._ascending)  # aligned under the field column
        layout.addLayout(form)

        self._table.currentIndexChanged.connect(self._rebuild_columns)
        self._rebuild_columns()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if step is not None:
            self._table.setCurrentText(step.table)
            if self._column.findText(step.column) < 0 and step.column:
                # Keep an out-of-schema column visible (and selectable) instead of silently
                # replacing it with the first item — the user must SEE the stale value to fix it.
                self._column.addItem(step.column)
            self._column.setCurrentText(step.column)
            self._ascending.setChecked(step.ascending)

    def _rebuild_columns(self) -> None:
        self._column.clear()
        self._column.addItems(_cols(self._table.currentText()))

    def step(self) -> SortStep:
        """Return the configured sort step."""
        return SortStep(
            table=self._table.currentText(),
            column=self._column.currentText(),
            ascending=self._ascending.isChecked(),
        )


class DeriveStepDialog(QDialog):
    """Configure a derive step: add a computed column (year/month/split/case)."""

    def __init__(self, step: DeriveStep | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Derive column step")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(SectionLabel("Derive column step"))

        form = QFormLayout()
        self._table = QComboBox()
        self._table.addItems(list(STORE_TABLES))
        self._source = QComboBox()
        self._op = QComboBox()
        for label, op in _DERIVE_OPS:
            self._op.addItem(label, op)
        self._target = QLineEdit()
        self._target.setPlaceholderText("(auto: <source>_<op>)")
        form.addRow("Table", self._table)
        form.addRow("Source column", self._source)
        form.addRow("Operation", self._op)
        form.addRow("New column", self._target)
        layout.addLayout(form)

        self._table.currentIndexChanged.connect(self._rebuild_columns)
        self._source.currentTextChanged.connect(self._suggest_target)
        self._op.currentIndexChanged.connect(self._suggest_target)
        self._rebuild_columns()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if step is not None:
            self._table.setCurrentText(step.table)
            self._source.setCurrentText(step.source)
            op_index = self._op.findData(step.op)
            if op_index >= 0:
                self._op.setCurrentIndex(op_index)
            self._target.setText(step.target)

    def _rebuild_columns(self) -> None:
        self._source.clear()
        self._source.addItems(_cols(self._table.currentText()))

    def _suggest_target(self) -> None:
        source = self._source.currentText()
        if source:
            self._target.setText(f"{source}_{self._op.currentData()}")

    def step(self) -> DeriveStep:
        """Return the configured derive step (blank target derives ``<source>_<op>``)."""
        return DeriveStep(
            table=self._table.currentText(),
            source=self._source.currentText(),
            target=self._target.text().strip(),
            op=self._op.currentData(),
        )


class AggregateStepDialog(QDialog):
    """Configure an aggregate step: group + count rows into a new summary table."""

    _NONE = "(none)"

    def __init__(self, step: AggregateStep | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Aggregate step")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(SectionLabel("Aggregate (group & count) step"))

        self._table = QComboBox()
        self._table.addItems(list(STORE_TABLES))
        table_form = QFormLayout()
        table_form.addRow("Table", self._table)
        layout.addLayout(table_form)

        layout.addWidget(QLabel("Group by columns"))
        # Default: nothing grouped yet; a new table resets to no group-by columns. Kept a direct
        # child of the top-level VBox: _rebuild_columns swaps it in place there.
        self._columns = _checkable_columns(_cols(self._table.currentText()), set())
        layout.addWidget(self._columns)

        form = QFormLayout()
        self._distinct = QComboBox()
        self._out = QLineEdit()
        self._out.setPlaceholderText("(auto: <table>_by_<columns>)")
        form.addRow("Also count distinct", self._distinct)
        form.addRow("Output table", self._out)
        layout.addLayout(form)

        self._table.currentIndexChanged.connect(self._rebuild_columns)
        self._rebuild_distinct()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if step is not None:
            self._table.setCurrentText(step.table)  # may rebuild the column list
            _set_checks(self._columns, step.group_by)
            if step.count_distinct:
                self._distinct.setCurrentText(step.count_distinct)
            self._out.setText(step.out_table)

    def _rebuild_columns(self) -> None:
        new = _checkable_columns(_cols(self._table.currentText()), set())
        old = self._columns
        layout = self.layout()
        if layout is not None:
            layout.replaceWidget(old, new)
        self._columns = new
        old.setParent(None)  # detach now so the old list can't paint as a ghost
        old.deleteLater()
        self._rebuild_distinct()

    def _rebuild_distinct(self) -> None:
        self._distinct.clear()
        self._distinct.addItem(self._NONE)
        self._distinct.addItems(_cols(self._table.currentText()))

    def step(self) -> AggregateStep:
        """Return the configured aggregate step."""
        distinct = self._distinct.currentText()
        return AggregateStep(
            table=self._table.currentText(),
            group_by=_checked_columns(self._columns),
            count_distinct=None if distinct == self._NONE else distinct,
            out_table=self._out.text().strip(),
        )


class ClassifyStepDialog(QDialog):
    """Configure a classify step: label a name column as company / individual / unknown."""

    def __init__(self, step: ClassifyStep | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Classify entity type step")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(SectionLabel("Classify entity type step"))

        form = QFormLayout()
        self._table = QComboBox()
        self._table.addItems(list(STORE_TABLES))
        self._column = QComboBox()
        self._target = QLineEdit()
        self._target.setPlaceholderText("(auto: <column>_type)")
        self._method = QComboBox()
        for label, value in _classify_methods():
            self._method.addItem(label, value)
        self._mode = QComboBox()
        for label, value in _COMBINE_MODES:
            self._mode.addItem(label, value)
        self._separator = QLineEdit()
        self._separator.setPlaceholderText('e.g. "; " to classify each concatenated party')
        form.addRow("Table", self._table)
        form.addRow("Name column", self._column)
        form.addRow("Type column", self._target)
        form.addRow("Method", self._method)
        form.addRow("Multi-party mode", self._mode)
        form.addRow("Split separator", self._separator)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._table.currentIndexChanged.connect(self._rebuild_columns)
        self._column.currentTextChanged.connect(self._suggest_for_column)
        self._rebuild_columns()

        if step is not None:
            self._table.setCurrentText(step.table)
            self._column.setCurrentText(step.column)
            if step.target:
                self._target.setText(step.target)
            self._method.setCurrentIndex(max(0, self._method.findData(step.method)))
            self._mode.setCurrentIndex(max(0, self._mode.findData(step.mode)))
            if step.separator:
                self._separator.setText(step.separator)

    def _rebuild_columns(self) -> None:
        self._column.clear()
        self._column.addItems(_cols(self._table.currentText()))

    def _suggest_for_column(self, column: str) -> None:
        if column:
            self._target.setText(f"{column}_type")
            self._separator.setText("; " if column.endswith("_names") else "")

    def step(self) -> ClassifyStep:
        """Return the configured classify step (blank target derives ``<column>_type``)."""
        column = self._column.currentText()
        target = self._target.text().strip()
        stored_target = "" if target == f"{column}_type" else target
        return ClassifyStep(
            table=self._table.currentText(),
            column=column,
            target=stored_target,
            method=self._method.currentData(),
            mode=self._mode.currentData(),
            separator=self._separator.text(),
        )


class CompareStepDialog(QDialog):
    """Configure a compare step: match two columns and flag, drop, or keep matches."""

    def __init__(  # noqa: PLR0915 - linear widget assembly
        self, step: CompareStep | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Compare columns step")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(SectionLabel("Compare columns step"))

        form = QFormLayout()
        self._table = QComboBox()
        self._table.addItems(list(STORE_TABLES))
        self._left = QComboBox()
        self._right = QComboBox()
        self._method = QComboBox()
        for label, value in _COMPARE_METHODS:
            self._method.addItem(label, value)
        self._scorer = QComboBox()
        self._scorer.addItems(scorer_names())
        self._threshold = QSpinBox()
        self._threshold.setRange(0, 100)
        self._threshold.setValue(90)
        self._action = QComboBox()
        for label, value in _COMPARE_ACTIONS:
            self._action.addItem(label, value)
        self._emit_score = QCheckBox("Add match-score column (similarity 0–100)")
        self._review = QSpinBox()
        self._review.setRange(0, 100)
        self._review.setSpecialValueText("Off")  # 0 = no review flagging
        form.addRow("Table", self._table)
        form.addRow("Left column", self._left)
        form.addRow("Right column", self._right)
        form.addRow("Method", self._method)
        form.addRow("Scorer (fuzzy)", self._scorer)
        form.addRow("Threshold (fuzzy)", self._threshold)
        form.addRow("Action", self._action)
        form.addRow("", self._emit_score)
        form.addRow("Flag review below", self._review)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._table.currentIndexChanged.connect(self._rebuild_columns)
        self._rebuild_columns()

        if step is not None:
            self._table.setCurrentText(step.table)
            self._left.setCurrentText(step.left)
            self._right.setCurrentText(step.right)
            self._method.setCurrentIndex(max(0, self._method.findData(step.method)))
            self._scorer.setCurrentText(step.scorer)
            self._threshold.setValue(step.threshold)
            self._action.setCurrentIndex(max(0, self._action.findData(step.action)))
            self._emit_score.setChecked(step.emit_score)
            self._review.setValue(step.review_threshold)
        # ``target`` (custom flag-column name) has no widget — carry it through on edit.
        self._carry_target = step.target if step is not None else ""

    def _rebuild_columns(self) -> None:
        cols = _cols(self._table.currentText())
        for combo in (self._left, self._right):
            combo.clear()
            combo.addItems(cols)

    def step(self) -> CompareStep:
        """Return the configured compare step."""
        return CompareStep(
            table=self._table.currentText(),
            left=self._left.currentText(),
            right=self._right.currentText(),
            target=self._carry_target,
            method=self._method.currentData(),
            scorer=self._scorer.currentText(),
            threshold=self._threshold.value(),
            action=self._action.currentData(),
            emit_score=self._emit_score.isChecked(),
            review_threshold=self._review.value(),
        )


class TransferTypeStepDialog(QDialog):
    """Configure the transfer-type preset: keep only a chosen assignor→assignee pairing."""

    def __init__(self, step: TransferTypeStep | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Transfer type step")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(SectionLabel("Transfer type (assignor → assignee) step"))

        form = QFormLayout()
        self._table = QComboBox()
        self._table.addItems(list(STORE_TABLES))
        self._table.setCurrentText("flat")
        self._assignor_col = QComboBox()
        self._assignee_col = QComboBox()
        self._assignor_type = QComboBox()
        self._assignor_type.addItems(_ENTITY_TYPES)
        self._assignee_type = QComboBox()
        self._assignee_type.addItems(_ENTITY_TYPES)
        self._method = QComboBox()
        for label, value in _classify_methods():
            self._method.addItem(label, value)
        form.addRow("Table", self._table)
        form.addRow("Assignor column", self._assignor_col)
        form.addRow("Assignee column", self._assignee_col)
        form.addRow("Assignor is", self._assignor_type)
        form.addRow("Assignee is", self._assignee_type)
        form.addRow("Method", self._method)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._table.currentIndexChanged.connect(self._rebuild_columns)
        self._rebuild_columns()

        if step is not None:
            self._table.setCurrentText(step.table)
            self._assignor_col.setCurrentText(step.assignor_column)
            self._assignee_col.setCurrentText(step.assignee_column)
            self._assignor_type.setCurrentText(step.assignor_type)
            self._assignee_type.setCurrentText(step.assignee_type)
            self._method.setCurrentIndex(max(0, self._method.findData(step.method)))

    def _rebuild_columns(self) -> None:
        cols = _cols(self._table.currentText())
        for combo, default in (
            (self._assignor_col, "assignor_names"),
            (self._assignee_col, "assignee_names"),
        ):
            combo.clear()
            combo.addItems(cols)
            if default in cols:
                combo.setCurrentText(default)

    def step(self) -> TransferTypeStep:
        """Return the configured transfer-type step."""
        return TransferTypeStep(
            table=self._table.currentText(),
            assignor_column=self._assignor_col.currentText(),
            assignee_column=self._assignee_col.currentText(),
            assignor_type=self._assignor_type.currentText(),
            assignee_type=self._assignee_type.currentText(),
            method=self._method.currentData(),
        )


class ReferenceMatchStepDialog(QDialog):
    """Configure a reference-match step: match a name column against a disambiguated reference."""

    def __init__(  # noqa: PLR0915 - linear widget assembly
        self, step: ReferenceMatchStep | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Match against reference step")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(SectionLabel("Match against disambiguated reference"))

        form = QFormLayout()
        self._table = QComboBox()
        self._table.addItems(list(STORE_TABLES))
        self._table.setCurrentText("flat")
        self._column = QComboBox()
        self._reference = QLineEdit()
        self._reference.setPlaceholderText("reference file (.tsv / .csv / .parquet)…")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._pick_reference)
        build = QPushButton("Build compact…")
        build.clicked.connect(self._build_reference)
        ref_row = QHBoxLayout()
        ref_row.addWidget(self._reference, 1)
        ref_row.addWidget(browse)
        ref_row.addWidget(build)
        self._name_column = QLineEdit("disambig_assignee_organization")
        self._id_column = QLineEdit()
        self._id_column.setPlaceholderText("(optional, e.g. assignee_id)")
        self._scorer = QComboBox()
        self._scorer.addItems(scorer_names())
        self._threshold = QSpinBox()
        self._threshold.setRange(0, 100)
        self._threshold.setValue(90)
        self._mode = QComboBox()
        for label, value in _REFERENCE_MODES:
            self._mode.addItem(label, value)
        self._action = QComboBox()
        for label, value in _REFERENCE_ACTIONS:
            self._action.addItem(label, value)
        self._emit_score = QCheckBox("Add match-score column (confidence 0–100)")
        self._review = QSpinBox()
        self._review.setRange(0, 100)
        self._review.setSpecialValueText("Off")  # 0 = no review flagging

        form.addRow("Table", self._table)
        form.addRow("Name column", self._column)
        form.addRow("Reference file", ref_row)
        form.addRow("Reference name column", self._name_column)
        form.addRow("Reference id column", self._id_column)
        form.addRow("Scorer", self._scorer)
        form.addRow("Threshold", self._threshold)
        form.addRow("Multi-party mode", self._mode)
        form.addRow("Action", self._action)
        form.addRow("", self._emit_score)
        form.addRow("Flag review below", self._review)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._table.currentIndexChanged.connect(self._rebuild_columns)
        self._rebuild_columns()

        if step is not None:
            self._table.setCurrentText(step.table)
            self._column.setCurrentText(step.column)
            self._reference.setText(step.reference_path)
            self._name_column.setText(step.name_column)
            self._id_column.setText(step.id_column)
            self._scorer.setCurrentText(step.scorer)
            self._threshold.setValue(step.threshold)
            self._mode.setCurrentIndex(max(0, self._mode.findData(step.mode)))
            self._action.setCurrentIndex(max(0, self._action.findData(step.action)))
            self._emit_score.setChecked(step.emit_score)
            self._review.setValue(step.review_threshold)
        # Spec-supported fields without widgets here — carry them through so an edit in this
        # dialog doesn't silently reset custom output names or file parsing options.
        self._carry_target = step.target if step is not None else ""
        self._carry_matched_target = step.matched_target if step is not None else ""
        self._carry_id_target = step.id_target if step is not None else ""
        self._carry_separator = step.separator if step is not None else ""
        self._carry_delimiter = step.delimiter if step is not None else ""

    def _rebuild_columns(self) -> None:
        self._column.clear()
        cols = _cols(self._table.currentText())
        self._column.addItems(cols)
        if "assignor_names" in cols:
            self._column.setCurrentText("assignor_names")

    def _pick_reference(self) -> None:
        state = UiStateStore()  # stateless per call; shared with the batch dialog's dirs
        path, _ = QFileDialog.getOpenFileName(
            self, "Reference file", state.last_dir("reference"), _REFERENCE_FILTER
        )
        if path:
            self._reference.setText(path)
            state.set_last_dir("reference", str(Path(path).parent))

    def _build_reference(self) -> None:
        src, _ = QFileDialog.getOpenFileName(
            self, "Big reference to compact (TSV/CSV)", "", _REFERENCE_FILTER
        )
        if not src:
            return
        dst, _ = QFileDialog.getSaveFileName(
            self, "Save compact reference", "reference.parquet", "Parquet (*.parquet)"
        )
        if not dst:
            return
        id_field = self._id_column.text().strip()
        try:
            count = extract_distinct_reference(
                Path(src),
                Path(dst),
                # Blank fields auto-detect the columns; a wrong name still falls back to detection.
                name_column=self._name_column.text().strip(),
                id_column=id_field or None,
            )
        except (OSError, ValueError, KeyError) as exc:
            QMessageBox.warning(self, "Build failed", f"Could not build compact reference:\n{exc}")
            return
        self._reference.setText(dst)
        # Point the step at the compact file's canonical columns — set the id field only if the
        # build actually produced one (org-only inputs have no id column).
        built_cols = reference_columns(Path(dst))
        self._name_column.setText("organization")
        self._id_column.setText("assignee_id" if "assignee_id" in built_cols else "")
        self.setWindowTitle(f"Match against reference step — built {count:,} orgs")

    def step(self) -> ReferenceMatchStep:
        """Return the configured reference-match step."""
        return ReferenceMatchStep(
            table=self._table.currentText(),
            column=self._column.currentText(),
            reference_path=self._reference.text().strip(),
            name_column=self._name_column.text().strip() or "disambig_assignee_organization",
            id_column=self._id_column.text().strip(),
            target=self._carry_target,
            matched_target=self._carry_matched_target,
            id_target=self._carry_id_target,
            scorer=self._scorer.currentText(),
            threshold=self._threshold.value(),
            separator=self._carry_separator,
            mode=self._mode.currentData(),
            delimiter=self._carry_delimiter,
            action=self._action.currentData(),
            emit_score=self._emit_score.isChecked(),
            review_threshold=self._review.value(),
        )


class FetchCpcStepDialog(QDialog):
    """Configure a fetch-CPC step: attach CPC codes to a patent-number column (grant-routed)."""

    def __init__(self, step: FetchCpcStep | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Fetch CPC step")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(SectionLabel("Attach CPC codes (exact grant join)"))

        form = QFormLayout()
        self._table = QComboBox()
        self._table.addItems(list(STORE_TABLES))
        self._table.setCurrentText("flat")
        self._column = QComboBox()
        self._kind_column = QComboBox()
        form.addRow("Table", self._table)
        form.addRow("Patent-number column", self._column)
        form.addRow("Kind-code column", self._kind_column)
        layout.addLayout(form)

        # Explanatory help is inserted once by attach_step_note() from the shared _STEP_HELP source.
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._table.currentIndexChanged.connect(self._rebuild_columns)
        self._rebuild_columns()
        if step is not None:
            self._table.setCurrentText(step.table)
            self._column.setCurrentText(step.column)
            self._kind_column.setCurrentText(step.kind_column)

    def _rebuild_columns(self) -> None:
        cols = _cols(self._table.currentText())
        for combo, default in ((self._column, "doc_number"), (self._kind_column, "doc_kind")):
            combo.clear()
            combo.addItems(cols)
            if default in cols:
                combo.setCurrentText(default)

    def step(self) -> FetchCpcStep:
        """Return the configured fetch-CPC step."""
        return FetchCpcStep(
            table=self._table.currentText(),
            column=self._column.currentText() or "doc_number",
            kind_column=self._kind_column.currentText() or "doc_kind",
        )


_CPC_FILE_FILTER = "CPC export (*.csv *.tsv *.xlsx *.parquet);;All files (*)"


class AttachCpcFileStepDialog(QDialog):
    """Configure an attach-CPC-from-file step: join CPC from a PatSeer/CSV/Parquet export."""

    def __init__(
        self, step: AttachCpcFileStep | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Attach CPC from file step")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(SectionLabel("Attach CPC codes from a file (PatSeer / CSV / Parquet)"))

        form = QFormLayout()
        self._table = QComboBox()
        self._table.addItems(list(STORE_TABLES))
        self._table.setCurrentText("flat")
        self._column = QComboBox()
        self._kind_column = QComboBox()
        self._source = QLineEdit()
        self._source.setPlaceholderText("CPC export file (.csv / .tsv / .xlsx / .parquet)…")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._pick_source)
        src_row = QHBoxLayout()
        src_row.addWidget(self._source, 1)
        src_row.addWidget(browse)
        self._patent_column = QLineEdit("Publication Number")
        self._code_column = QLineEdit("CPC")
        self._separator = QLineEdit(";")
        self._separator.setPlaceholderText("(blank = one CPC per row)")

        form.addRow("Table", self._table)
        form.addRow("Patent-number column", self._column)
        form.addRow("Kind-code column", self._kind_column)
        form.addRow("CPC file", src_row)
        form.addRow("File patent column", self._patent_column)
        form.addRow("File CPC column", self._code_column)
        form.addRow("CPC separator", self._separator)
        layout.addLayout(form)

        # Explanatory help is inserted once by attach_step_note() from the shared _STEP_HELP source.
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._table.currentIndexChanged.connect(self._rebuild_columns)
        self._rebuild_columns()
        if step is not None:
            self._table.setCurrentText(step.table)
            self._column.setCurrentText(step.column)
            self._kind_column.setCurrentText(step.kind_column)
            self._source.setText(step.source_path)
            self._patent_column.setText(step.patent_column)
            self._code_column.setText(step.code_column)
            self._separator.setText(step.separator)

    def _rebuild_columns(self) -> None:
        cols = _cols(self._table.currentText())
        for combo, default in ((self._column, "doc_number"), (self._kind_column, "doc_kind")):
            combo.clear()
            combo.addItems(cols)
            if default in cols:
                combo.setCurrentText(default)

    def _pick_source(self) -> None:
        state = UiStateStore()
        path, _ = QFileDialog.getOpenFileName(
            self, "CPC export file", state.last_dir("cpc_file"), _CPC_FILE_FILTER
        )
        if path:
            self._source.setText(path)
            state.set_last_dir("cpc_file", str(Path(path).parent))

    def step(self) -> AttachCpcFileStep:
        """Return the configured attach-CPC-from-file step."""
        return AttachCpcFileStep(
            table=self._table.currentText(),
            column=self._column.currentText() or "doc_number",
            kind_column=self._kind_column.currentText() or "doc_kind",
            source_path=self._source.text().strip(),
            patent_column=self._patent_column.text().strip() or "Publication Number",
            code_column=self._code_column.text().strip() or "CPC",
            separator=self._separator.text(),
        )


class CpcMatchStepDialog(QDialog):
    """Configure a CPC-match step: rank buyers per sales-package (portfolio) patent."""

    def __init__(  # noqa: PLR0915 - linear widget assembly
        self, step: CpcMatchStep | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("CPC match step")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(SectionLabel("Match portfolio to buyers by CPC overlap"))

        form = QFormLayout()
        self._table = QComboBox()
        self._table.addItems(list(STORE_TABLES))
        self._table.setCurrentText("flat")
        self._mode = QComboBox()
        self._mode.addItem("Portfolio patent list (fetch its CPC)", "patent_list")
        self._mode.addItem("Pre-built CPC footprint file", "footprint_file")
        self._portfolio = QLineEdit()
        self._portfolio.setPlaceholderText("portfolio patents (.txt) or footprint (.csv/.parquet)…")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._pick_portfolio)
        pf_row = QHBoxLayout()
        pf_row.addWidget(self._portfolio, 1)
        pf_row.addWidget(browse)
        self._buyer_column = QComboBox()
        self._buyer_column.setEditable(True)
        self._number_column = QComboBox()
        self._number_column.setEditable(True)
        self._kind_column = QComboBox()
        self._kind_column.setEditable(True)
        self._date_column = QComboBox()
        self._date_column.setEditable(True)

        self._emit_class_matches = QCheckBox(
            "Also output per-class matches (portfolio × buyer patent × CPC class)"
        )

        form.addRow("Table", self._table)
        form.addRow("Portfolio input", self._mode)
        form.addRow("Portfolio file", pf_row)
        form.addRow("Buyer column", self._buyer_column)
        form.addRow("Patent-number column", self._number_column)
        form.addRow("Kind-code column", self._kind_column)
        form.addRow("Date column", self._date_column)
        form.addRow("", self._emit_class_matches)
        layout.addLayout(form)

        # Explanatory help is inserted once by attach_step_note() from the shared _STEP_HELP source.
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._table.currentIndexChanged.connect(self._rebuild_columns)
        self._rebuild_columns()
        if step is not None:
            self._table.setCurrentText(step.table)
            self._mode.setCurrentIndex(max(0, self._mode.findData(step.portfolio_mode)))
            self._portfolio.setText(step.portfolio_path)
            self._buyer_column.setCurrentText(step.buyer_column)
            self._number_column.setCurrentText(step.number_column)
            self._kind_column.setCurrentText(step.kind_column)
            self._date_column.setCurrentText(step.date_column)
            self._emit_class_matches.setChecked(step.emit_class_matches)
        # Custom output-table names have no widgets — carry them through so an edit doesn't
        # silently rename the tables a later export step targets.
        defaults = CpcMatchStep(table="flat")
        self._carry_out = step.out_table if step is not None else defaults.out_table
        self._carry_overall = step.overall_table if step is not None else defaults.overall_table
        self._carry_class = (
            step.class_match_table if step is not None else defaults.class_match_table
        )

    def _rebuild_columns(self) -> None:
        cols = _cols(self._table.currentText())
        for combo, default in (
            (self._buyer_column, "assignee_names_canonical"),
            (self._number_column, "doc_number"),
            (self._kind_column, "doc_kind"),
            (self._date_column, "transaction_date"),
        ):
            current = combo.currentText()
            combo.clear()
            combo.addItems(cols)
            combo.setCurrentText(current or (default if default in cols else ""))

    def _pick_portfolio(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Portfolio file", "", "Portfolio (*.txt *.csv *.parquet);;All files (*)"
        )
        if path:
            self._portfolio.setText(path)

    def step(self) -> CpcMatchStep:
        """Return the configured CPC-match step."""
        return CpcMatchStep(
            table=self._table.currentText(),
            portfolio_mode=self._mode.currentData(),
            portfolio_path=self._portfolio.text().strip(),
            buyer_column=self._buyer_column.currentText() or "assignee_names_canonical",
            number_column=self._number_column.currentText() or "doc_number",
            kind_column=self._kind_column.currentText() or "doc_kind",
            date_column=self._date_column.currentText() or "transaction_date",
            out_table=self._carry_out,
            overall_table=self._carry_overall,
            emit_class_matches=self._emit_class_matches.isChecked(),
            class_match_table=self._carry_class,
        )


_StepDialog = (
    FilterStepDialog
    | NormalizeStepDialog
    | DedupeStepDialog
    | SelectStepDialog
    | SortStepDialog
    | DeriveStepDialog
    | AggregateStepDialog
    | ClassifyStepDialog
    | CompareStepDialog
    | TransferTypeStepDialog
    | ReferenceMatchStepDialog
    | FetchCpcStepDialog
    | AttachCpcFileStepDialog
    | CpcMatchStepDialog
    | ExportStepDialog
)

# Step kinds offered by the "Add step" menu (order = menu order).
_STEP_DIALOGS: list[tuple[str, type[_StepDialog]]] = [
    ("Filter…", FilterStepDialog),
    ("Normalize names…", NormalizeStepDialog),
    ("Classify entity type…", ClassifyStepDialog),
    ("Compare columns…", CompareStepDialog),
    ("Transfer type (firm→firm…)…", TransferTypeStepDialog),
    ("Match against reference…", ReferenceMatchStepDialog),
    ("Fetch CPC codes…", FetchCpcStepDialog),
    ("Attach CPC from file…", AttachCpcFileStepDialog),
    ("CPC match to portfolio…", CpcMatchStepDialog),
    ("Deduplicate…", DedupeStepDialog),
    ("Select columns…", SelectStepDialog),
    ("Sort…", SortStepDialog),
    ("Derive column…", DeriveStepDialog),
    ("Aggregate (group & count)…", AggregateStepDialog),
    ("Export…", ExportStepDialog),
]

# Maps a step dataclass to the dialog that edits it (for double-click editing).
_EDIT_DIALOGS: dict[type[BatchStep], type[_StepDialog]] = {
    FilterStep: FilterStepDialog,
    NormalizeStep: NormalizeStepDialog,
    ClassifyStep: ClassifyStepDialog,
    CompareStep: CompareStepDialog,
    TransferTypeStep: TransferTypeStepDialog,
    ReferenceMatchStep: ReferenceMatchStepDialog,
    FetchCpcStep: FetchCpcStepDialog,
    AttachCpcFileStep: AttachCpcFileStepDialog,
    CpcMatchStep: CpcMatchStepDialog,
    DedupeStep: DedupeStepDialog,
    SelectStep: SelectStepDialog,
    SortStep: SortStepDialog,
    DeriveStep: DeriveStepDialog,
    AggregateStep: AggregateStepDialog,
    ExportStep: ExportStepDialog,
}
# Reverse of _EDIT_DIALOGS, so the opener can look up a dialog's step kind for its help note.
_STEP_TYPE_BY_DIALOG: dict[type[_StepDialog], type[BatchStep]] = {
    dialog: step_type for step_type, dialog in _EDIT_DIALOGS.items()
}


def attach_step_note(dialog: QDialog, step_type: type[BatchStep]) -> None:
    """Insert the shared step help under a step-editor dialog's title, from the one source.

    Driven off ``help_content.step_note_text`` (the same text the Help panel shows), so the note a
    user reads *while configuring* a step can never drift from the panel — and every step dialog
    gets one, replacing the few hand-written per-dialog notes. Inserted at layout index 1, i.e.
    directly under the title ``SectionLabel`` every step dialog adds first.
    """
    text = step_note_text(step_type)
    layout = dialog.layout()
    if not text or not isinstance(layout, QVBoxLayout) or layout.count() == 0:
        return
    note = QLabel(text)
    note.setWordWrap(True)
    note.setProperty("role", "hint")
    layout.insertWidget(1, note)


class BatchDialog(QDialog):
    """Build, save, and run batch-processing templates with a live console."""

    def __init__(
        self,
        store: BatchTemplateStore,
        memory_store: EntityMemoryStore | None = None,
        parent: QWidget | None = None,
        *,
        cpc_store: CpcConfigStore | None = None,
        ui_state: UiStateStore | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Batch processing")
        # Make this a normal top-level window (not a close-only modal dialog) so the window manager
        # gives it working minimize/maximize/restore controls. It is opened non-modally.
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.resize(980, 680)
        self._store = store
        self._memory_store = memory_store if memory_store is not None else EntityMemoryStore()
        self._cpc_store = cpc_store if cpc_store is not None else CpcConfigStore()
        self._ui_state = ui_state if ui_state is not None else UiStateStore()
        self._memory: EntityMemory | None = None  # populated at run time from the store
        self._steps: list[BatchStep] = []
        self._description = ""  # a loaded/imported template's own embedded help, carried opaquely
        self._completed = 0  # files finished this run (drives the determinate progress bar)
        self._thread: QThread | None = None
        self._worker: QObject | None = None  # BatchWorker (run) or CallWorker (preview)
        self._close_after_run = False  # close the dialog once the active run's thread stops
        self._log_emitter = LogEmitter()
        self._log_handler = QtLogHandler(self._log_emitter)
        self._log_emitter.message.connect(self._append_console)

        root = QHBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)
        root.addLayout(self._build_config_column(), 3)
        root.addLayout(self._build_console_column(), 4)
        self._help_panel = HelpPanel()
        self._help_panel.setVisible(False)  # opened on demand via the Help toggle button
        root.addWidget(self._help_panel, 3)
        self._help_toggle.toggled.connect(self._toggle_help)
        self._steps_list.currentRowChanged.connect(lambda _row: self._update_help())
        self._saved.currentIndexChanged.connect(lambda _index: self._update_help())
        self._template_name.textChanged.connect(lambda _text: self._update_help())

        self._reload_templates()

    # -- config column -----------------------------------------------------
    def _build_config_column(self) -> QVBoxLayout:  # noqa: PLR0915 - linear widget assembly
        column = QVBoxLayout()
        column.setSpacing(10)

        column.addWidget(SectionLabel("Template"))
        self._template_name = QLineEdit()
        self._template_name.setPlaceholderText("template name")
        self._saved = QComboBox()
        self._saved.currentIndexChanged.connect(self._load_selected_template)
        name_row = QHBoxLayout()
        name_row.addWidget(self._template_name, 1)
        save_btn = QPushButton("Save")
        save_btn.setProperty("primary", "true")
        save_btn.clicked.connect(self._save_template)
        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(self._delete_template)
        name_row.addWidget(save_btn)
        name_row.addWidget(delete_btn)
        self._help_toggle = QPushButton("Help")
        self._help_toggle.setCheckable(True)
        self._help_toggle.setToolTip(
            "Show what the selected template — or the step selected below — does"
        )
        name_row.addWidget(self._help_toggle)
        column.addLayout(name_row)
        column.addWidget(self._saved)

        example_btn = QPushButton("New from example ▾")
        example_menu = QMenu(example_btn)
        for name, factory in _PRESETS:
            action = QAction(name, example_menu)
            action.triggered.connect(lambda _checked=False, f=factory: self._apply_template(f()))
            example_menu.addAction(action)
        example_btn.setMenu(example_menu)
        column.addLayout(
            self._button_row(
                ("Duplicate", self._duplicate_template),
                ("Import…", self._import_template),
                ("Export…", self._export_template),
            )
        )
        column.addWidget(example_btn)

        column.addWidget(SectionLabel("Inputs (xml / zip / parquet files, or dataset folders)"))
        self._inputs = QListWidget()
        self._inputs.setProperty("panel", "true")
        # Extended selection so Remove can drop several rows at once (Ctrl/Shift-click).
        self._inputs.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # Weighted 1:2:2 with the field tree and steps list — the working areas get the space.
        column.addWidget(self._inputs, 1)
        column.addLayout(
            self._button_row(
                ("Add files…", self._add_files),
                ("Add folder…", self._add_folder),
                ("Add by pattern…", self._add_by_pattern),
                ("Remove", self._remove_input),
                ("Clear", self._clear_inputs),
            )
        )

        column.addWidget(SectionLabel("Load — fields & record cap"))
        self._max = QSpinBox()
        self._max.setRange(0, _MAX_RECORDS_CAP)
        self._max.setSpecialValueText("All records")
        self._max.setValue(0)
        max_form = QFormLayout()
        max_form.addRow("Max records", self._max)
        column.addLayout(max_form)
        self._fields = FieldTree()
        column.addWidget(self._fields, 2)

        column.addWidget(SectionLabel("Steps (double-click to edit)"))
        self._steps_list = QListWidget()
        self._steps_list.setProperty("panel", "true")
        self._steps_list.itemDoubleClicked.connect(self._edit_step)
        column.addWidget(self._steps_list, 2)

        add_btn = QPushButton("Add step ▾")
        add_btn.setProperty("primary", "true")
        menu = QMenu(add_btn)
        for label, dialog_cls in _STEP_DIALOGS:
            action = QAction(label, menu)
            action.triggered.connect(lambda _checked=False, cls=dialog_cls: self._add_step(cls))
            menu.addAction(action)
        add_btn.setMenu(menu)
        steps_row = QHBoxLayout()
        steps_row.addWidget(add_btn)
        for label, slot in (
            ("↑", lambda: self._move_step(-1)),
            ("↓", lambda: self._move_step(1)),
            ("Duplicate", self._duplicate_step),
            ("Enable/Disable", self._toggle_step),
            ("Remove", self._remove_step),
        ):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, s=slot: s())
            steps_row.addWidget(button)
        steps_row.addStretch(1)
        column.addLayout(steps_row)
        return column

    # -- console column ----------------------------------------------------
    def _build_console_column(self) -> QVBoxLayout:  # noqa: PLR0915 - linear widget assembly
        column = QVBoxLayout()
        column.setSpacing(10)

        column.addWidget(SectionLabel("Output"))
        self._out_dir = QLineEdit()
        self._out_dir.setPlaceholderText("output folder…")
        # Managed default: remembered last output dir, else <cwd>/data/out.
        self._out_dir.setText(self._ui_state.last_dir("output") or str(Path.cwd() / "data" / "out"))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._choose_output)
        out_row = QHBoxLayout()
        out_row.addWidget(self._out_dir, 1)
        out_row.addWidget(browse)
        column.addLayout(out_row)

        self._workers = QSpinBox()
        self._workers.setRange(1, 32)
        self._workers.setValue(1)
        workers_form = QFormLayout()
        workers_form.addRow("Workers (1 = sequential)", self._workers)
        column.addLayout(workers_form)

        # CPC: a per-run network opt-in for fetch_cpc steps, plus quick access to the source config.
        self._allow_network = QCheckBox("Allow network for CPC fetch this run")
        cpc_btn = QPushButton("CPC data source…")
        cpc_btn.clicked.connect(self._open_cpc_settings)
        cpc_row = QHBoxLayout()
        cpc_row.addWidget(self._allow_network, 1)
        cpc_row.addWidget(cpc_btn)
        column.addLayout(cpc_row)

        # Trace: dump each enabled step's output to <source>/steps/ for manual review.
        self._save_steps = QCheckBox("Save each step's output (for review)")
        self._save_steps.setToolTip(
            "Write every enabled step's resulting table(s) to <run>/<source>/steps/"
            "NN_<table>.<ext> so you can open and check each intermediate."
        )
        # Its own format (independent of the Export step); defaults to Parquet (lossless, compact).
        self._trace_format = QComboBox()
        for label, value in _FORMATS:
            self._trace_format.addItem(label, value)
        self._trace_format.setToolTip("Format for the saved step outputs (default Parquet).")
        self._trace_format.setEnabled(False)
        self._save_steps.toggled.connect(self._trace_format.setEnabled)
        trace_row = QHBoxLayout()
        trace_row.addWidget(self._save_steps)
        trace_row.addWidget(QLabel("as"))
        trace_row.addWidget(self._trace_format)
        trace_row.addStretch(1)
        column.addLayout(trace_row)

        # Convert mode: write outputs directly into the chosen folder, named by source file.
        self._flat_output = QCheckBox("Convert mode: one folder, files named by source")
        self._flat_output.setToolTip(
            "Write outputs straight into the output folder as <source>_<table>.<ext> "
            "(or <source>.<ext> when a single table is written; no timestamped run subfolder, "
            "no manifest/summary — just a _convert_index.csv breadcrumb). Ideal with the "
            "'Convert to Parquet' template."
        )
        column.addWidget(self._flat_output)
        # Convert-mode options: existing-file policy + subfolder mirroring (enabled with the mode).
        self._convert_policy = QComboBox()
        for label, value in (
            ("Overwrite", "overwrite"),
            ("Skip existing", "skip"),
            ("Keep both", "unique"),
        ):
            self._convert_policy.addItem(label, value)
        self._convert_policy.setToolTip(
            "When a target file already exists: Overwrite it, Skip it (leave it — makes a bulk "
            "conversion resumable), or Keep both (never clobber; append ' (1)')."
        )
        self._mirror_tree = QCheckBox("Mirror source subfolders")
        self._mirror_tree.setToolTip(
            "Recreate each source's subfolder position (under the inputs' common parent) beneath "
            "the output folder, instead of flattening everything into one folder."
        )
        for widget in (self._convert_policy, self._mirror_tree):
            widget.setEnabled(False)
            self._flat_output.toggled.connect(widget.setEnabled)
        convert_row = QHBoxLayout()
        convert_row.addWidget(QLabel("when exists:"))
        convert_row.addWidget(self._convert_policy)
        convert_row.addWidget(self._mirror_tree)
        convert_row.addStretch(1)
        column.addLayout(convert_row)

        column.addWidget(SectionLabel("Console"))
        self._console = QPlainTextEdit()
        self._console.setReadOnly(True)
        self._console.setProperty("role", "console")
        column.addWidget(self._console, 1)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        column.addWidget(self._progress)

        self._preview_btn = QPushButton("Preview…")
        self._preview_btn.clicked.connect(self._preview)
        self._run_btn = QPushButton("Run batch")
        self._run_btn.setProperty("primary", "true")
        self._run_btn.clicked.connect(self._run)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setVisible(False)  # only shown while a batch run is active
        self._cancel_btn.clicked.connect(self._cancel)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        run_row = QHBoxLayout()
        run_row.addStretch(1)
        run_row.addWidget(self._preview_btn)
        run_row.addWidget(self._run_btn)
        run_row.addWidget(self._cancel_btn)
        run_row.addWidget(close_btn)
        column.addLayout(run_row)
        return column

    @staticmethod
    def _button_row(*buttons: tuple[str, Any]) -> QHBoxLayout:
        row = QHBoxLayout()
        for label, slot in buttons:
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, s=slot: s())
            row.addWidget(button)
        row.addStretch(1)
        return row

    # -- template build / persistence -------------------------------------
    def template(self) -> BatchTemplate:
        """Build a :class:`BatchTemplate` from the current UI state."""
        return BatchTemplate(
            name=self._template_name.text().strip() or "batch",
            load=LoadConfig(
                limit=self._max.value() or None, columns=self._fields.selected_columns()
            ),
            steps=list(self._steps),
            description=self._description,  # preserved so save/export round-trips it
        )

    def _reload_templates(self) -> None:
        self._saved.blockSignals(True)
        self._saved.clear()
        self._saved.addItem("— saved templates —", None)
        for template in self._store.load():
            self._saved.addItem(template.name, template.name)
        self._saved.blockSignals(False)

    def _apply_template(self, template: BatchTemplate) -> None:
        """Load a template into the UI (name, record cap, field selection, and steps)."""
        self._template_name.setText(template.name)
        self._max.setValue(template.load.limit or 0)
        self._fields.set_selected_columns(template.load.columns)
        self._steps = [copy.deepcopy(s) for s in template.steps]
        self._description = template.description
        self._refresh_steps_list()

    def _load_selected_template(self) -> None:
        name = self._saved.currentData()
        if not name:
            return
        match = next((t for t in self._store.load() if t.name == name), None)
        if match is not None:
            self._apply_template(match)

    def _duplicate_template(self) -> None:
        template = self.template()
        template.name = f"{template.name} copy"
        self._store.add(template)
        self._reload_templates()
        self._template_name.setText(template.name)
        self._append_console(f"Duplicated as '{template.name}'")

    def _export_template(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export template",
            f"{self._template_name.text().strip() or 'template'}.json",
            "JSON (*.json)",
        )
        if path:
            dump_templates([self.template()], Path(path))
            self._append_console(f"Exported template to {Path(path).name}")

    def _import_template(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import template", "", "JSON (*.json)")
        if not path:
            return
        try:
            templates = load_templates(Path(path))
        except (TemplateFormatError, OSError) as exc:
            # Imported files are routinely hand- or LLM-authored; a bad one must show its
            # problem, not escape the Qt slot and abort the whole app.
            QMessageBox.warning(self, "Import failed", str(exc))
            return
        if templates:
            self._apply_template(templates[0])
            self._append_console(f"Imported template '{templates[0].name}'")

    def _save_template(self) -> None:
        template = self.template()
        self._store.add(template)
        self._reload_templates()
        self._append_console(f"Saved template '{template.name}'")

    def _delete_template(self) -> None:
        # Prefer the selected saved template; the free-typed name is only a fallback so a
        # half-typed new name can't silently delete an unrelated saved template.
        name = self._saved.currentData() or self._template_name.text().strip()
        if not name:
            return
        answer = QMessageBox.question(self, "Delete template", f"Delete saved template '{name}'?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._store.delete(name)
        self._reload_templates()
        self._append_console(f"Deleted template '{name}'")

    # -- inputs ------------------------------------------------------------
    def _current_inputs(self) -> set[str]:
        """The set of input paths already in the list (for de-duping additions)."""
        return {self._inputs.item(i).text() for i in range(self._inputs.count())}  # type: ignore[union-attr]

    def _add_input_paths(self, paths: Sequence[str | Path]) -> int:
        """Add each path once (skipping any already present); return how many were newly added."""
        existing = self._current_inputs()
        added = 0
        for path in paths:
            text = str(path)
            if text not in existing:
                self._inputs.addItem(text)
                existing.add(text)
                added += 1
        return added

    def _add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Add input files",
            self._ui_state.last_dir("input"),
            "USPTO input (*.xml *.zip *.parquet *.arrow *.feather *.csv)"
            ";;USPTO assignment (*.xml *.zip)"
            ";;Processed data (*.parquet *.arrow *.feather *.csv)"
            ";;All files (*)",
        )
        self._add_input_paths(paths)
        if paths:
            self._ui_state.set_last_dir("input", str(Path(paths[0]).parent))

    def _add_folder(self) -> None:
        """Add a folder: a parsed dataset folder is one input; else scan it for input files.

        A pre-parsed dataset folder (``flat.parquet`` / ``flat.arrow`` …) is added as a single
        input for :func:`open_dataset`. Any other folder is walked **recursively** for ``.xml`` /
        ``.zip`` and processed data files (parquet/arrow/feather/csv), each added as its own input —
        so a folder of USPTO dumps (or of processed extracts) converts in one go.
        """
        path = QFileDialog.getExistingDirectory(
            self,
            "Add dataset folder or a folder of input files",
            self._ui_state.last_dir("input"),
        )
        if not path:
            return
        folder = Path(path)
        if is_dataset_dir(folder):
            self._add_input_paths([path])
        else:
            files = sorted(
                p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in _INPUT_SUFFIXES
            )
            if not files:
                QMessageBox.information(
                    self,
                    "Nothing to add",
                    "No .xml/.zip or processed data files, and no parsed dataset, were found in:"
                    f"\n{folder}",
                )
                return
            self._add_input_paths(files)
        self._ui_state.set_last_dir("input", path)

    def _add_by_pattern(self) -> None:
        """Pick a folder, then add only the files whose names match a pattern.

        The pattern is a glob (``*_flat*``, ``*.parquet``) when it has a glob metacharacter,
        otherwise a case-insensitive substring (``_flat``). Matches are restricted to the accepted
        input suffixes and de-duped against the list. See :func:`match_input_files`.
        """
        path = QFileDialog.getExistingDirectory(
            self,
            "Choose a folder to pick input files from",
            self._ui_state.last_dir("input"),
        )
        if not path:
            return
        folder = Path(path)
        pattern, ok = QInputDialog.getText(
            self,
            "Add by filename pattern",
            "Filename pattern (glob like *_flat*.parquet, or a substring like _flat):",
            text="*_flat*",
        )
        if not ok:
            return
        matches = match_input_files(folder, pattern.strip(), _INPUT_SUFFIXES)
        if not matches:
            QMessageBox.information(
                self,
                "No files matched",
                f"No input files matched '{pattern.strip()}' in:\n{folder}",
            )
            return
        added = self._add_input_paths(matches)
        self._ui_state.set_last_dir("input", path)
        skipped = len(matches) - added
        note = f"Added {added} file(s)" + (f" ({skipped} already in the list)" if skipped else "")
        self._append_console(note)

    def _clear_inputs(self) -> None:
        self._inputs.clear()

    def _remove_input(self) -> None:
        for item in self._inputs.selectedItems():
            self._inputs.takeItem(self._inputs.row(item))

    def _schema_base(self) -> dict[str, list[str]] | None:
        """Actual schemas of any dataset-folder inputs, for schema-aware validation/pickers."""
        return inputs_schema_base(self._input_paths())

    def _input_paths(self) -> list[Path]:
        return [Path(self._inputs.item(i).text()) for i in range(self._inputs.count())]  # type: ignore[union-attr]

    # -- steps -------------------------------------------------------------
    def _load(self) -> LoadConfig:
        return LoadConfig(limit=self._max.value() or None, columns=self._fields.selected_columns())

    def _open_step_dialog(
        self, dialog_cls: type[_StepDialog], step: BatchStep | None, index: int
    ) -> BatchStep | None:
        """Open a step dialog with the columns available at ``index`` in scope; return new step."""
        global _available_ctx  # noqa: PLW0603 - modal, single-threaded schema context for dialogs
        previous = _available_ctx
        _available_ctx = columns_after(self._load(), self._steps, index, base=self._schema_base())
        try:
            dialog = dialog_cls(step, parent=self) if step is not None else dialog_cls(parent=self)  # type: ignore[arg-type]
            step_type = _STEP_TYPE_BY_DIALOG.get(dialog_cls)
            if step_type is not None:
                attach_step_note(dialog, step_type)
            return dialog.step() if dialog.exec() == QDialog.DialogCode.Accepted else None
        finally:
            _available_ctx = previous

    def _add_step(self, dialog_cls: type[_StepDialog]) -> None:
        row = self._steps_list.currentRow()
        index = (
            row + 1 if 0 <= row < len(self._steps) else len(self._steps)
        )  # insert after selection
        new = self._open_step_dialog(dialog_cls, None, index)
        if new is not None:
            self._steps.insert(index, new)
            self._refresh_steps_list()
            self._steps_list.setCurrentRow(index)

    def _edit_step(self, item: QListWidgetItem) -> None:
        row = self._steps_list.row(item)
        if not (0 <= row < len(self._steps)):
            return
        step = self._steps[row]
        dialog_cls = _EDIT_DIALOGS.get(type(step))
        if dialog_cls is None:
            return
        new = self._open_step_dialog(dialog_cls, step, row)
        if new is not None:
            new.enabled = step.enabled  # editing preserves the enabled flag
            self._steps[row] = new
            self._refresh_steps_list()
            self._steps_list.setCurrentRow(row)

    def _remove_step(self) -> None:
        row = self._steps_list.currentRow()
        if 0 <= row < len(self._steps):
            del self._steps[row]
            self._refresh_steps_list()

    def _move_step(self, delta: int) -> None:
        row = self._steps_list.currentRow()
        target = row + delta
        if 0 <= row < len(self._steps) and 0 <= target < len(self._steps):
            self._steps[row], self._steps[target] = self._steps[target], self._steps[row]
            self._refresh_steps_list()
            self._steps_list.setCurrentRow(target)

    def _duplicate_step(self) -> None:
        row = self._steps_list.currentRow()
        if 0 <= row < len(self._steps):
            self._steps.insert(row + 1, copy.deepcopy(self._steps[row]))
            self._refresh_steps_list()
            self._steps_list.setCurrentRow(row + 1)

    def _toggle_step(self) -> None:
        row = self._steps_list.currentRow()
        if 0 <= row < len(self._steps):
            self._steps[row].enabled = not self._steps[row].enabled
            self._refresh_steps_list()
            self._steps_list.setCurrentRow(row)

    def _refresh_steps_list(self) -> None:
        self._steps_list.clear()
        warned = _warnings_by_step(
            validate_template(self._load(), self._steps, base=self._schema_base())
        )
        for index, step in enumerate(self._steps, start=1):
            badge = "⚠ " if index in warned else ""
            text = f"{index}. {badge}{_describe_step(step)}"
            if not step.enabled:
                text += "   (disabled)"
            item = QListWidgetItem(text)
            if not step.enabled:
                font = item.font()
                font.setStrikeOut(True)
                item.setFont(font)
                item.setForeground(Qt.GlobalColor.gray)
            if index in warned:
                item.setToolTip("\n".join(warned[index]))
            self._steps_list.addItem(item)
        # Rebuilding the list drops the selection (no step focused) — refresh to the
        # template-level view rather than leaving stale step help on screen.
        self._update_help()

    # -- help panel ----------------------------------------------------------
    def _toggle_help(self, checked: bool) -> None:
        self._help_panel.setVisible(checked)
        # Give the panel real width instead of squeezing it out of the existing two columns
        # (a help panel that has to wrap every long word onto its own line is unreadable).
        self.resize(self.width() + _HELP_PANEL_WIDTH * (1 if checked else -1), self.height())
        if checked:
            self._update_help()

    def _update_help(self) -> None:
        """Refresh the Help panel to match the current selection, if it's open.

        Gated on the toggle button's checked state rather than ``self._help_panel.isVisible()``
        — a widget only reports visible once its top-level window is shown, so that guard would
        never fire before the dialog's first ``show()`` (and never in a headless test).
        """
        if not self._help_toggle.isChecked():
            return
        row = self._steps_list.currentRow()
        if 0 <= row < len(self._steps):
            self._help_panel.show_step(self._steps[row])
        else:
            self._help_panel.show_template(
                self._template_name.text(), self._steps, self._description
            )

    # -- run ---------------------------------------------------------------
    def _choose_output(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Choose output folder", self._ui_state.last_dir("output")
        )
        if path:
            self._out_dir.setText(path)
            self._ui_state.set_last_dir("output", path)

    def _open_cpc_settings(self) -> None:
        CpcSettingsDialog(self._cpc_store, self).exec()

    def _cpc_ctx(self) -> CpcRunContext:
        """Build the per-run CPC context from the saved project config + the network checkbox."""
        return CpcRunContext(
            config=self._cpc_store.load(), allow_network=self._allow_network.isChecked()
        )

    def _run(self) -> None:
        if self._thread is not None:
            return
        inputs = self._input_paths()
        out_text = self._out_dir.text().strip()
        if not inputs or not out_text:
            self._append_console("Add at least one input and an output folder.")
            return
        template = self.template()
        self._console.clear()
        warnings = validate_template(template.load, template.steps, base=self._schema_base())
        if warnings:  # run_batch re-emits each warning to the console; only prompt here
            proceed = QMessageBox.question(
                self,
                "Validation warnings",
                f"The pipeline has {len(warnings)} warning(s). Run anyway?",
            )
            if proceed != QMessageBox.StandardButton.Yes:
                return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._append_console(f"Running '{template.name}' over {len(inputs)} input(s)…")

        logging.getLogger(_CORE_LOGGER).addHandler(self._log_handler)
        self._completed = 0
        self._progress.setRange(0, len(inputs))  # determinate: files completed
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._run_btn.setEnabled(False)
        self._preview_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._cancel_btn.setVisible(True)

        self._memory = self._memory_store.load()
        thread = QThread(self)
        worker = BatchWorker(
            template,
            inputs,
            Path(out_text),
            workers=self._workers.value(),
            timestamp=timestamp,
            memory=self._memory,
            cpc_ctx=self._cpc_ctx(),
            trace_steps=self._save_steps.isChecked(),
            trace_fmt=self._trace_format.currentData(),
            flat_output=self._flat_output.isChecked(),
            existing=self._convert_policy.currentData(),
            mirror_tree=self._mirror_tree.isChecked(),
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.batch_event.connect(self._on_event)
        worker.finished.connect(self._on_finished)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._cleanup)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _cancel(self) -> None:
        """Request cooperative cancellation of the running batch (takes effect between files)."""
        if isinstance(self._worker, BatchWorker):
            self._worker.cancel()
            self._cancel_btn.setEnabled(False)
            self._append_console("Cancelling — finishing the file(s) in flight…")

    def _on_event(self, event: object) -> None:
        if isinstance(event, BatchEvent):
            self._append_console(event.message, event.level)
            if event.kind == "file_done":
                self._completed += 1
                self._progress.setValue(self._completed)

    def _on_finished(self, result: object) -> None:
        self._finish_run()
        batch = result if isinstance(result, BatchResult) else None
        # Persist only when the run actually learned something. Sequential runs record into the
        # shared memory's ``learned``; parallel runs carry it per file (``apply_learned`` merges
        # silently), so check both. A run with no normalize steps must not rewrite the store.
        learned = bool(self._memory is not None and self._memory.learned) or bool(
            batch is not None and any(r.learned for r in batch.results)
        )
        if self._memory is not None and learned:
            self._memory_store.save(self._memory)
            canonicals, aliases = self._memory.counts()
            self._append_console(f"Entity memory: {canonicals:,} canonicals, {aliases:,} aliases")
        if isinstance(result, BatchResult):
            note = " (cancelled)" if result.cancelled else ""
            self._append_console(
                f"Done: {result.succeeded} succeeded, {result.failed} failed{note}.",
                "error" if result.failed else "success",
            )
        else:
            self._append_console("Done.")

    # -- preview -----------------------------------------------------------
    def _preview(self) -> None:
        if self._thread is not None:
            return
        inputs = self._input_paths()
        if not inputs:
            self._append_console("Add at least one input to preview.")
            return
        template = self.template()
        source = inputs[0]
        self._append_console(
            f"Previewing '{template.name}' on {source.name} (first {_PREVIEW_LIMIT:,} records)…"
        )
        self._progress.setRange(0, 0)  # indeterminate while the sample is computed
        self._progress.setVisible(True)
        self._preview_btn.setEnabled(False)
        self._run_btn.setEnabled(False)

        thread = QThread(self)
        cpc_ctx = self._cpc_ctx()
        worker = CallWorker(
            lambda: run_preview(
                template, source, limit=_PREVIEW_LIMIT, describe=_describe_step, cpc_ctx=cpc_ctx
            )
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_preview_ready)
        worker.failed.connect(self._on_preview_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._cleanup)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _on_preview_ready(self, result: object) -> None:
        self._finish_preview()
        if not isinstance(result, tuple):
            return
        tables, stats = result
        self._append_console(f"Preview ready ({len(tables)} table(s)).", "success")
        PreviewDialog(tables, stats, parent=self).exec()

    def _on_preview_failed(self, message: str) -> None:
        self._finish_preview()
        self._append_console(f"Preview error: {message}", "error")

    def _finish_preview(self) -> None:
        self._progress.setVisible(False)
        self._preview_btn.setEnabled(True)
        self._run_btn.setEnabled(True)

    def _on_failed(self, message: str) -> None:
        self._finish_run()
        self._append_console(f"Batch error: {message}")

    def _finish_run(self) -> None:
        self._progress.setVisible(False)
        self._run_btn.setEnabled(True)
        self._preview_btn.setEnabled(True)
        self._cancel_btn.setVisible(False)
        logging.getLogger(_CORE_LOGGER).removeHandler(self._log_handler)

    def _cleanup(self) -> None:
        if self._thread is not None:
            self._thread.deleteLater()
        self._thread = None
        self._worker = None
        if self._close_after_run:
            self._close_after_run = False
            self.close()

    # -- lifetime ------------------------------------------------------------
    def _confirm_close(self) -> bool:
        """True when it is safe to close now; otherwise offer to cancel and close on finish.

        The dialog must never be destroyed while a worker thread is alive (Qt aborts on a
        running ``QThread``'s destruction), so a close during a run only *requests* it: the
        run is cancelled and ``_cleanup`` closes the window once the thread stops.
        """
        if self._thread is None:
            return True
        answer = QMessageBox.question(
            self,
            "Batch running",
            "A run is in progress. Cancel it and close this window when it stops?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._cancel()
            self._close_after_run = True
        return False

    def closeEvent(self, a0: QCloseEvent | None) -> None:
        """Guard window-manager closes; drop the log handler on a real close."""
        if not self._confirm_close():
            if a0 is not None:
                a0.ignore()
            return
        logging.getLogger(_CORE_LOGGER).removeHandler(self._log_handler)  # idempotent
        super().closeEvent(a0)

    def reject(self) -> None:
        """Route the Close button and Esc through the same running-batch guard."""
        if self._confirm_close():
            logging.getLogger(_CORE_LOGGER).removeHandler(self._log_handler)  # idempotent
            super().reject()

    def _append_console(self, text: str, level: str = "info") -> None:
        color = {"error": "#d9534f", "success": "#4a934a", "warning": "#c77d29"}.get(level)
        if color:
            self._console.appendHtml(f'<span style="color:{color};">{html.escape(text)}</span>')
        else:
            self._console.appendPlainText(text)


def _warnings_by_step(warnings: list[str]) -> dict[int, list[str]]:
    """Group ``validate_template`` warnings by their 1-based step number (``"Step N …"``)."""
    grouped: dict[int, list[str]] = {}
    for message in warnings:
        match = re.match(r"Step (\d+)", message)
        if match:
            grouped.setdefault(int(match.group(1)), []).append(message)
    return grouped
