"""A dialog to browse saved queries and apply or delete them."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from uspto_assignments import Query

from ..settings import QueryStore
from .page import SectionLabel


class QueryDialog(QDialog):
    """List saved queries; Apply returns the chosen one, Delete removes it from the store."""

    def __init__(self, store: QueryStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Saved queries")
        self.setMinimumWidth(420)
        self._store = store
        self._selected: Query | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        layout.addWidget(SectionLabel("Saved queries"))

        self._list = QListWidget()
        layout.addWidget(self._list)

        buttons = QHBoxLayout()
        self._apply = QPushButton("Apply")
        self._apply.setProperty("primary", "true")
        self._apply.clicked.connect(self._on_apply)
        self._delete = QPushButton("Delete")
        self._delete.clicked.connect(self._on_delete)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons.addWidget(self._apply)
        buttons.addWidget(self._delete)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self._reload()

    def selected_query(self) -> Query | None:
        """The query the user chose to apply (None if cancelled)."""
        return self._selected

    def _reload(self) -> None:
        self._list.clear()
        for query in self._store.load():
            item = QListWidgetItem(f"{query.name}   ·   {query.table}")
            item.setData(Qt.ItemDataRole.UserRole, query)
            self._list.addItem(item)
        has_items = self._list.count() > 0
        self._apply.setEnabled(has_items)
        self._delete.setEnabled(has_items)
        if has_items:
            self._list.setCurrentRow(0)

    def _current_query(self) -> Query | None:
        item = self._list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _on_apply(self) -> None:
        query = self._current_query()
        if query is not None:
            self._selected = query
            self.accept()

    def _on_delete(self) -> None:
        query = self._current_query()
        if query is not None:
            self._store.delete(query.name)
            self._reload()
