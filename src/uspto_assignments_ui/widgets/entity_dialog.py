"""Edit the normalization entity memory: view/search canonicals and aliases, correct, import/export.

Edits mutate a **working copy** of the memory; ``Save`` persists it via the store, ``Cancel``
discards. Structural edits (rename/merge/delete) go through the memory's edit API, which rebuilds
the fuzzy block index so ``resolve()`` keeps matching afterwards.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from uspto_assignments import EntityMemory

from ..models import EntityAliasModel
from ..settings import EntityMemoryStore
from .data_table import DataTable
from .page import SectionLabel

_IMPORT_FILTER = "Entity names (*.csv *.json *.txt);;All files (*)"
_SEARCH_DEBOUNCE_MS = 250
_MAX_CANONICALS = 5000  # cap the visible canonical list so a 75k-entry memory stays responsive


def _search_box(placeholder: str) -> QLineEdit:
    box = QLineEdit()
    box.setPlaceholderText(placeholder)
    box.setClearButtonEnabled(True)
    return box


class EntityDialog(QDialog):
    """A tabbed editor for the entity memory (Canonicals + Aliases) with import/export/relocate."""

    def __init__(self, store: EntityMemoryStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Entity memory")
        # A normal top-level window (not a close-only modal dialog) so it minimizes/maximizes and
        # can be viewed alongside the batch window. Opened non-modally by the main window.
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.resize(720, 620)
        self._store = store
        self._memory = store.load()  # working copy; Save persists it, Cancel discards

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        layout.addWidget(SectionLabel("Entity memory"))
        self._counts = QLabel()
        layout.addWidget(self._counts)
        self._path_label = QLabel()
        self._path_label.setProperty("role", "hint")
        self._path_label.setWordWrap(True)
        layout.addWidget(self._path_label)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_canonicals_tab(), "Canonicals")
        self._tabs.addTab(self._build_aliases_tab(), "Aliases")
        layout.addWidget(self._tabs, 1)

        layout.addLayout(self._build_file_row())
        layout.addWidget(self._build_button_box())

        self._refresh()

    # -- canonicals tab ----------------------------------------------------
    def _build_canonicals_tab(self) -> QWidget:
        tab = QWidget()
        col = QVBoxLayout(tab)
        col.setContentsMargins(0, 8, 0, 0)
        col.setSpacing(8)
        self._canon_search = _search_box("search canonical names…")
        self._canon_timer = QTimer(self)
        self._canon_timer.setSingleShot(True)
        self._canon_timer.setInterval(_SEARCH_DEBOUNCE_MS)
        self._canon_search.textChanged.connect(lambda _t: self._canon_timer.start())
        self._canon_timer.timeout.connect(self._refresh_canonicals)
        col.addWidget(self._canon_search)

        self._canon_list = QListWidget()
        self._canon_list.setProperty("panel", "true")
        col.addWidget(self._canon_list, 1)
        self._canon_note = QLabel()
        self._canon_note.setProperty("role", "hint")
        col.addWidget(self._canon_note)

        col.addLayout(
            self._button_row(
                ("Add…", self._add_canonical),
                ("Rename…", self._rename_canonical),
                ("Merge…", self._merge_canonical),
                ("Delete", self._delete_canonical),
            )
        )
        return tab

    # -- aliases tab -------------------------------------------------------
    def _build_aliases_tab(self) -> QWidget:
        tab = QWidget()
        col = QVBoxLayout(tab)
        col.setContentsMargins(0, 8, 0, 0)
        col.setSpacing(8)
        self._alias_search = _search_box("search aliases or canonicals…")
        self._alias_timer = QTimer(self)
        self._alias_timer.setSingleShot(True)
        self._alias_timer.setInterval(_SEARCH_DEBOUNCE_MS)
        self._alias_search.textChanged.connect(lambda _t: self._alias_timer.start())
        self._alias_timer.timeout.connect(self._refresh_aliases)
        col.addWidget(self._alias_search)

        self._alias_model = EntityAliasModel(self._memory)
        self._alias_table = DataTable()
        self._alias_table.setModel(self._alias_model)
        col.addWidget(self._alias_table, 1)
        self._alias_note = QLabel()
        self._alias_note.setProperty("role", "hint")
        col.addWidget(self._alias_note)
        hint = QLabel("Double-click a canonical cell to reassign an alias.")
        hint.setProperty("role", "hint")
        col.addWidget(hint)
        col.addLayout(self._button_row(("Delete alias", self._delete_alias)))
        return tab

    # -- file + save/cancel rows -------------------------------------------
    def _build_file_row(self) -> QHBoxLayout:
        return self._button_row(
            ("Import…", self._import),
            ("Export…", self._export),
            ("Change location…", self._relocate),
            ("Clear", self._clear),
            primary="Import…",
            stretch=True,
        )

    def _build_button_box(self) -> QDialogButtonBox:
        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        save = box.button(QDialogButtonBox.StandardButton.Save)
        if save is not None:
            save.setProperty("primary", "true")
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)
        return box

    @staticmethod
    def _button_row(
        *buttons: tuple[str, object], primary: str = "", stretch: bool = False
    ) -> QHBoxLayout:
        row = QHBoxLayout()
        for label, slot in buttons:
            button = QPushButton(label)
            if label == primary:
                button.setProperty("primary", "true")
            button.clicked.connect(lambda _checked=False, s=slot: s())  # type: ignore[operator]
            row.addWidget(button)
        if stretch:
            row.addStretch(1)
        return row

    # -- refresh -----------------------------------------------------------
    def _refresh(self) -> None:
        canonicals, aliases = self._memory.counts()
        self._counts.setText(f"{canonicals:,} canonical names · {aliases:,} learned aliases")
        self._path_label.setText(f"Stored at: {self._store.path}")
        self._refresh_canonicals()
        self._alias_model.refresh()
        self._update_alias_note()

    def _refresh_aliases(self) -> None:
        self._alias_model.set_filter(self._alias_search.text())
        self._update_alias_note()

    def _update_alias_note(self) -> None:
        count = self._alias_model.rowCount()
        self._alias_note.setText(
            f"showing the first {count:,} matches — refine the search to see the rest"
            if self._alias_model.truncated
            else f"{count:,} match(es)"
        )

    def _refresh_canonicals(self) -> None:
        needle = self._canon_search.text().strip().lower()
        matches = [c for c in self._memory.canonicals if not needle or needle in c.lower()]
        shown = matches[:_MAX_CANONICALS]
        self._canon_list.clear()
        self._canon_list.addItems(shown)
        extra = len(matches) - len(shown)
        self._canon_note.setText(
            f"showing {len(shown):,} of {len(matches):,} matches (+{extra:,} more)"
            if extra > 0
            else f"{len(shown):,} match(es)"
        )

    def _selected_canonical(self) -> str | None:
        item = self._canon_list.currentItem()
        return item.text() if item is not None else None

    # -- canonical edits ---------------------------------------------------
    def _add_canonical(self) -> None:
        name, ok = QInputDialog.getText(self, "Add canonical", "Canonical name:")
        if ok and name.strip():
            self._memory.add_canonical(name.strip())
            self._refresh()

    def _rename_canonical(self) -> None:
        old = self._selected_canonical()
        if old is None:
            return
        new, ok = QInputDialog.getText(self, "Rename canonical", "New name:", text=old)
        if ok and new.strip():
            self._memory.rename_canonical(old, new.strip())
            self._refresh()

    def _merge_canonical(self) -> None:
        source = self._selected_canonical()
        if source is None:
            return
        target, ok = QInputDialog.getText(
            self, "Merge canonical", f"Merge '{source}' into (target canonical):"
        )
        if ok and target.strip():
            self._memory.merge_canonicals(source, target.strip())
            self._refresh()

    def _delete_canonical(self) -> None:
        name = self._selected_canonical()
        if name is not None:
            self._memory.delete_canonical(name)
            self._refresh()

    def _delete_alias(self) -> None:
        rows = sorted({i.row() for i in self._alias_table.selectedIndexes()})
        if rows:
            self._alias_model.delete_aliases(rows)
            self._refresh()

    # -- file operations ---------------------------------------------------
    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import entity names", "", _IMPORT_FILTER)
        if not path:
            return
        try:
            self._memory.seed_from_file(Path(path))
        except (OSError, ValueError) as exc:
            self._counts.setText(f"Import failed: {exc}")
            return
        self._refresh()

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export entity memory", "entities.json", "JSON (*.json)"
        )
        if path:
            self._memory.save(Path(path))

    def _relocate(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Entity memory location", str(self._store.path), "JSON (*.json)"
        )
        if path:
            self._store.relocate(Path(path))
            self._path_label.setText(f"Stored at: {self._store.path}")

    def _clear(self) -> None:
        self._memory = EntityMemory()
        self._alias_model = EntityAliasModel(self._memory)
        self._alias_table.setModel(self._alias_model)
        self._refresh()

    def _save(self) -> None:
        self._store.save(self._memory)
        self.accept()
