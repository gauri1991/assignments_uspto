"""A dialog to keep / reorder / rename / drop the columns of a loaded table."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .page import SectionLabel


class ColumnEditorDialog(QDialog):
    """Pick which columns to keep, in what order, and rename them; returns the plan on accept.

    A checkbox per source column includes/excludes it; **Move up/down** reorders; the second cell
    is an editable output name. :meth:`result` returns ``(kept_sources_in_order, {source: new})``.
    """

    def __init__(self, columns: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit columns")
        self.setMinimumSize(520, 420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(SectionLabel("Keep, reorder, and rename columns"))

        self._grid = QTableWidget(0, 2)
        self._grid.setHorizontalHeaderLabels(["Column (check = keep)", "Output name"])
        self._grid.setProperty("panel", "true")
        self._grid.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        vheader = self._grid.verticalHeader()
        if vheader is not None:
            vheader.setVisible(False)
        hheader = self._grid.horizontalHeader()
        if hheader is not None:
            hheader.setStretchLastSection(True)
            hheader.resizeSection(0, 240)  # room for the "Column (check = keep)" header
        for name in columns:
            row = self._grid.rowCount()
            self._grid.insertRow(row)
            source = QTableWidgetItem(name)
            source.setFlags(
                (source.flags() | Qt.ItemFlag.ItemIsUserCheckable) & ~Qt.ItemFlag.ItemIsEditable
            )
            source.setCheckState(Qt.CheckState.Checked)
            self._grid.setItem(row, 0, source)
            self._grid.setItem(row, 1, QTableWidgetItem(name))
        layout.addWidget(self._grid)

        move_row = QHBoxLayout()
        up = QPushButton("Move up")
        up.clicked.connect(lambda: self._move(-1))
        down = QPushButton("Move down")
        down.clicked.connect(lambda: self._move(1))
        move_row.addWidget(up)
        move_row.addWidget(down)
        move_row.addStretch(1)
        layout.addLayout(move_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

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

    def column_plan(self) -> tuple[list[str], dict[str, str]]:
        """Return ``(kept sources in order, {source: output name})`` for kept + renamed cols."""
        kept: list[str] = []
        renames: dict[str, str] = {}
        for row in range(self._grid.rowCount()):
            source_item = self._grid.item(row, 0)
            output_item = self._grid.item(row, 1)
            if source_item is None or source_item.checkState() != Qt.CheckState.Checked:
                continue
            source = source_item.text()
            output = (output_item.text().strip() if output_item else "") or source
            kept.append(source)
            if output != source:
                renames[source] = output
        return kept, renames
