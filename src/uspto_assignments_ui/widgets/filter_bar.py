"""A per-field filter builder: quick search, AND/OR combine, and add/remove filter clauses.

Value entry is an editable combo: for low-cardinality columns it is pre-filled with the distinct
values (pick or type); for everything else it behaves like a text box. Emits ``changed`` whenever
the effective filter changes; the owning panel reads :meth:`clauses`, :meth:`quick_search`, and
:meth:`combine` and applies them vectorized via :mod:`uspto_assignments.filters`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PyQt6.QtCore import QSignalBlocker, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from uspto_assignments import CombineMode
from uspto_assignments.filters import FilterClause, FilterOp, is_valueless

# Display label -> operator, in menu order.
_OPERATORS: list[tuple[str, FilterOp]] = [
    ("Contains", "contains"),
    ("Equals", "equals"),
    ("Not equals", "not_equals"),
    ("Starts with", "starts_with"),
    ("In range", "in_range"),
    ("Not empty", "not_empty"),
    ("Is empty", "is_empty"),
]
_COMBINES: list[tuple[str, CombineMode]] = [("Match all (AND)", "and"), ("Match any (OR)", "or")]
_QUICK_SEARCH_DEBOUNCE_MS = 250

DistinctProvider = Callable[[str], list[str] | None]


class FilterBar(QWidget):
    """Quick search + AND/OR combine + an add-clause row and a strip of active-filter chips."""

    changed = pyqtSignal()

    def __init__(
        self,
        columns: Sequence[str],
        distinct_provider: DistinctProvider | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("panel", "true")  # subtle frame around the filter area
        self._clauses: list[FilterClause] = []
        self._distinct_provider = distinct_provider

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        root.addWidget(self._build_search())
        root.addLayout(self._build_builder_row(columns))
        root.addLayout(self._build_chips_row())

        self._on_column_changed()
        self._render_chips()

    def _build_search(self) -> QLineEdit:
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search all columns…")
        self._search.setClearButtonEnabled(True)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_QUICK_SEARCH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self.changed)
        self._search.textChanged.connect(lambda _text: self._debounce.start())
        return self._search

    def _build_builder_row(self, columns: Sequence[str]) -> QHBoxLayout:
        builder = QHBoxLayout()
        builder.setSpacing(8)
        self._column = QComboBox()
        self._column.addItems(list(columns))
        self._column.currentIndexChanged.connect(self._on_column_changed)
        self._op = QComboBox()
        for label, op in _OPERATORS:
            self._op.addItem(label, op)
        self._op.currentIndexChanged.connect(self._sync_value_fields)
        self._value = QComboBox()
        self._value.setEditable(True)
        self._value.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        value_edit = self._value.lineEdit()
        if value_edit is not None:
            value_edit.setPlaceholderText("value")
            value_edit.returnPressed.connect(self._add_clause)
        self._value2 = QLineEdit()
        self._value2.setPlaceholderText("to")
        add_button = QPushButton("Add filter")
        add_button.setProperty("primary", "true")
        add_button.clicked.connect(self._add_clause)
        for widget in (self._column, self._op, self._value, self._value2):
            builder.addWidget(widget)
        builder.addWidget(add_button)
        return builder

    def _build_chips_row(self) -> QHBoxLayout:
        chips_row = QHBoxLayout()
        chips_row.setSpacing(6)
        self._combine = QComboBox()
        for label, mode in _COMBINES:
            self._combine.addItem(label, mode)
        self._combine.currentIndexChanged.connect(lambda _i: self.changed.emit())
        self._chips = QHBoxLayout()
        self._chips.setSpacing(6)
        chips_container = QWidget()
        chips_container.setLayout(self._chips)
        clear_button = QPushButton("Clear all")
        clear_button.clicked.connect(self._clear_all)
        chips_row.addWidget(QLabel("Combine"))
        chips_row.addWidget(self._combine)
        chips_row.addWidget(chips_container)
        chips_row.addStretch(1)
        chips_row.addWidget(clear_button)
        return chips_row

    # -- reads --------------------------------------------------------------
    def clauses(self) -> list[FilterClause]:
        """Return the active filter clauses."""
        return list(self._clauses)

    def quick_search(self) -> str | None:
        """Return the trimmed quick-search text, or None if empty."""
        return self._search.text().strip() or None

    def combine(self) -> CombineMode:
        """Return whether clauses are AND-ed ('and') or OR-ed ('or')."""
        return self._combine.currentData()

    def set_state(
        self,
        clauses: Sequence[FilterClause],
        combine: CombineMode,
        quick_search: str | None,
    ) -> None:
        """Replace the whole filter state (used when applying a saved query)."""
        self._clauses = list(clauses)
        with QSignalBlocker(self._search), QSignalBlocker(self._combine):
            self._search.setText(quick_search or "")
            index = self._combine.findData(combine)
            if index >= 0:
                self._combine.setCurrentIndex(index)
        self._render_chips()
        self.changed.emit()

    # -- internal ----------------------------------------------------------
    def _current_op(self) -> FilterOp:
        return self._op.currentData()

    def _on_column_changed(self) -> None:
        column = self._column.currentText()
        values = self._distinct_provider(column) if self._distinct_provider else None
        with QSignalBlocker(self._value):
            self._value.clear()
            if values:
                self._value.addItems(values)
            edit = self._value.lineEdit()
            if edit is not None:
                edit.clear()
        self._apply_smart_operator(column, categorical=values is not None)
        self._sync_value_fields()

    def _apply_smart_operator(self, column: str, *, categorical: bool) -> None:
        if column.endswith("_date"):
            default: FilterOp = "in_range"
        elif categorical:
            default = "equals"
        else:
            default = "contains"
        index = self._op.findData(default)
        if index >= 0:
            with QSignalBlocker(self._op):
                self._op.setCurrentIndex(index)

    def _sync_value_fields(self) -> None:
        op = self._current_op()
        self._value.setVisible(not is_valueless(op))
        self._value2.setVisible(op == "in_range")

    def _add_clause(self) -> None:
        op = self._current_op()
        value = self._value.currentText().strip()
        if not is_valueless(op) and not value:
            return
        self._clauses.append(
            FilterClause(
                column=self._column.currentText(),
                op=op,
                value=value,
                value2=self._value2.text().strip(),
            )
        )
        edit = self._value.lineEdit()
        if edit is not None:
            edit.clear()
        self._value2.clear()
        self._render_chips()
        self.changed.emit()

    def _remove_clause(self, clause: FilterClause) -> None:
        self._clauses.remove(clause)
        self._render_chips()
        self.changed.emit()

    def _clear_all(self) -> None:
        if not self._clauses and not self._search.text():
            return
        self._clauses.clear()
        self._search.clear()
        self._render_chips()
        self.changed.emit()

    def _render_chips(self) -> None:
        while self._chips.count():
            item = self._chips.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for clause in self._clauses:
            chip = QPushButton(f"{self._chip_text(clause)}   ×")  # × is typography, not an icon
            chip.setProperty("chip", "true")
            chip.clicked.connect(lambda _checked=False, c=clause: self._remove_clause(c))
            self._chips.addWidget(chip)
        self._chips.addStretch(1)

    @staticmethod
    def _chip_text(clause: FilterClause) -> str:
        label = next((lbl for lbl, op in _OPERATORS if op == clause.op), clause.op)
        if is_valueless(clause.op):
            return f"{clause.column} · {label}"
        if clause.op == "in_range":
            return f"{clause.column} · {clause.value}–{clause.value2}"
        return f"{clause.column} · {label} · {clause.value}"
