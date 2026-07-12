"""A Qt table model backed by a (memory-mapped) PyArrow table.

The key to staying responsive over millions of rows: the model never copies the data and never
filters row-by-row in Python. It holds the Arrow table plus a ``_view`` index array (the visible
rows, after filter/sort). Qt only queries the ~50 cells on screen, so ``data()`` is O(visible),
and filtering/sorting just swap the index array (computed vectorized in
:mod:`uspto_assignments.filters`).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import numpy.typing as npt
import pyarrow as pa
from PyQt6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
)

_Index = QModelIndex | QPersistentModelIndex
_DISPLAY = Qt.ItemDataRole.DisplayRole
_TOOLTIP = Qt.ItemDataRole.ToolTipRole


class ArrowTableModel(QAbstractTableModel):
    """Expose one PyArrow table to a ``QTableView`` through a swappable row-index view."""

    def __init__(self, table: pa.Table, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._table = table
        self._columns: list[str] = list(table.column_names)
        # Cache each column's ChunkedArray so ``data()`` avoids re-resolving it per cell.
        self._chunks = [table.column(i) for i in range(table.num_columns)]
        self._view: npt.NDArray[np.int64] = np.arange(table.num_rows, dtype=np.int64)

    # -- Qt model interface -------------------------------------------------
    def rowCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008 (Qt signature)
        return 0 if parent.isValid() else int(self._view.shape[0])

    def columnCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008 (Qt signature)
        return 0 if parent.isValid() else len(self._columns)

    def data(self, index: _Index, role: int = _DISPLAY) -> Any:
        if not index.isValid() or role not in (_DISPLAY, _TOOLTIP):
            return None
        source_row = int(self._view[index.row()])
        value = self._chunks[index.column()][source_row].as_py()
        return "" if value is None else str(value)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = _DISPLAY) -> Any:
        if role != _DISPLAY:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self._columns[section]
        return str(section + 1)  # 1-based visible row number

    # -- View (filter/sort) control ----------------------------------------
    def set_view(self, indices: pa.Array[Any] | npt.NDArray[np.int64]) -> None:
        """Replace the visible rows with ``indices`` (from filters.apply / sort_indices)."""
        new_view = (
            indices.to_numpy() if isinstance(indices, pa.Array) else np.asarray(indices, np.int64)
        )
        self.beginResetModel()
        self._view = new_view.astype(np.int64, copy=False)
        self.endResetModel()

    def reset_view(self) -> None:
        """Show all rows in natural order."""
        self.beginResetModel()
        self._view = np.arange(self._table.num_rows, dtype=np.int64)
        self.endResetModel()

    def source_row(self, view_row: int) -> int:
        """Map a visible row index to its row index in the underlying table."""
        return int(self._view[view_row])

    def source_rows(self, view_rows: list[int]) -> list[int]:
        """Map visible row indices to underlying-table row indices."""
        return [int(self._view[r]) for r in view_rows]

    # -- Accessors ----------------------------------------------------------
    @property
    def table(self) -> pa.Table:
        """The underlying PyArrow table."""
        return self._table

    @property
    def columns(self) -> list[str]:
        """Column names in order."""
        return list(self._columns)

    @property
    def visible_count(self) -> int:
        """Number of rows currently visible (after filtering)."""
        return int(self._view.shape[0])
