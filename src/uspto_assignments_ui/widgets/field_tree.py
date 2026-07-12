"""A checkable table→columns tree for selecting which fields to load/keep.

Built from the static schema (``columns_for``), so it works before any file is opened. Used by
both the load dialog and the batch-processing dialog.
"""

from __future__ import annotations

from collections.abc import Sequence

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QTreeWidget, QTreeWidgetItem, QWidget

from uspto_assignments import STORE_TABLES, columns_for

_PARENT_FLAGS = (
    Qt.ItemFlag.ItemIsUserCheckable
    | Qt.ItemFlag.ItemIsEnabled
    | Qt.ItemFlag.ItemIsAutoTristate  # parent reflects/propagates child checks
    | Qt.ItemFlag.ItemIsSelectable
)
_CHILD_FLAGS = (
    Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
)


class FieldTree(QTreeWidget):
    """A collapsed, all-checked tree of ``table → columns`` for the given tables."""

    def __init__(self, tables: Sequence[str] = STORE_TABLES, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        for name in tables:
            table_item = QTreeWidgetItem([name])
            table_item.setFlags(_PARENT_FLAGS)
            for column in columns_for(name):
                column_item = QTreeWidgetItem([column])
                column_item.setFlags(_CHILD_FLAGS)
                column_item.setCheckState(0, Qt.CheckState.Checked)
                table_item.addChild(column_item)
            table_item.setCheckState(0, Qt.CheckState.Checked)
            self.addTopLevelItem(table_item)
        self.collapseAll()

    def set_selected_columns(self, columns: dict[str, list[str]]) -> None:
        """Check exactly the columns in ``columns`` (a table absent from the map is left as-is)."""
        if not columns:
            return
        for i in range(self.topLevelItemCount()):
            table_item = self.topLevelItem(i)
            if table_item is None or table_item.text(0) not in columns:
                continue
            wanted = set(columns[table_item.text(0)])
            for j in range(table_item.childCount()):
                child = table_item.child(j)
                if child is not None:
                    on = child.text(0) in wanted
                    child.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def selected_columns(self) -> dict[str, list[str]]:
        """Return ``table -> checked columns`` (empty list = table fully unchecked)."""
        columns: dict[str, list[str]] = {}
        for i in range(self.topLevelItemCount()):
            table_item = self.topLevelItem(i)
            if table_item is None:
                continue
            columns[table_item.text(0)] = [
                child.text(0)
                for j in range(table_item.childCount())
                if (child := table_item.child(j)) is not None
                and child.checkState(0) == Qt.CheckState.Checked
            ]
        return columns
