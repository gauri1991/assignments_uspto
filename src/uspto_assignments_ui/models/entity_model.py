"""An editable Qt table model over an :class:`EntityMemory`'s alias→canonical mappings.

``ArrowTableModel`` is read-only and Arrow-bound, so the entity-memory editor needs its own model:
it materializes ``(alias, canonical)`` rows from a working-copy memory, supports a substring filter,
and writes edits back through the memory's edit API (which rebuilds the block index).
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt

from uspto_assignments import EntityMemory

_HEADERS = ("Alias (cleaned key)", "Canonical name")
_MAX_ROWS = 5000  # cap the visible slice so filtering a 75k-entry memory stays responsive


class EntityAliasModel(QAbstractTableModel):
    """Editable table of a memory's aliases; editing the canonical cell reassigns the alias."""

    def __init__(self, memory: EntityMemory, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._memory = memory
        self._filter = ""
        self._rows: list[tuple[str, str]] = []
        self._truncated = False
        self._materialize()

    # -- data ---------------------------------------------------------------
    def _materialize(self) -> None:
        needle = self._filter.lower()
        rows = sorted(
            (a, c)
            for a, c in self._memory.aliases.items()
            if not needle or needle in a.lower() or needle in c.lower()
        )
        self._truncated = len(rows) > _MAX_ROWS
        self._rows = rows[:_MAX_ROWS]

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        return len(self._rows)

    def columnCount(self, parent: QModelIndex | None = None) -> int:
        return len(_HEADERS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return _HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return self._rows[index.row()][index.column()]
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.isValid() and index.column() == 1:  # only the canonical cell is editable
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if role != Qt.ItemDataRole.EditRole or not index.isValid() or index.column() != 1:
            return False
        new_canonical = str(value).strip()
        if not new_canonical:
            return False
        alias = self._rows[index.row()][0]
        self._memory.set_alias(alias, new_canonical)  # reassign (adds the canonical if new)
        self._rows[index.row()] = (alias, new_canonical)
        self.dataChanged.emit(index, index)
        return True

    # -- editing helpers ----------------------------------------------------
    @property
    def truncated(self) -> bool:
        """True when the current filter matched more than the display cap."""
        return self._truncated

    def set_filter(self, text: str) -> None:
        """Show only aliases/canonicals containing ``text`` (case-insensitive)."""
        self.beginResetModel()
        self._filter = text.strip()
        self._materialize()
        self.endResetModel()

    def refresh(self) -> None:
        """Re-read rows from the memory (after external edits)."""
        self.beginResetModel()
        self._materialize()
        self.endResetModel()

    def alias_at(self, row: int) -> str | None:
        """The alias key at ``row`` (for deletion), or None if out of range."""
        return self._rows[row][0] if 0 <= row < len(self._rows) else None

    def delete_aliases(self, rows: list[int]) -> None:
        """Delete the aliases at the given view rows from the memory."""
        for alias in [a for row in rows if (a := self.alias_at(row)) is not None]:
            self._memory.delete_alias(alias)
        self.refresh()
