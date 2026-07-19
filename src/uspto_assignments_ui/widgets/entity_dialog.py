"""Edit the normalization entity memory: view/search canonicals and aliases, correct, import/export.

Edits mutate a **working copy** of the memory; ``Save`` persists it via the store, ``Cancel``
discards. Structural edits (rename/merge/delete) go through the memory's edit API, which rebuilds
the fuzzy block index so ``resolve()`` keeps matching afterwards.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from uspto_assignments import EntityMemory, ReferenceGazetteer, build_reference, tag_memory

from ..models import EntityAliasModel
from ..settings import EntityMemoryStore
from ..workers import CallWorker
from .data_table import DataTable
from .page import SectionLabel

_IMPORT_FILTER = "Entity names (*.csv *.json *.txt);;All files (*)"
_REFERENCE_FILTER = "Reference (*.tsv *.csv *.parquet);;All files (*)"
_REFERENCE_NAME_COLUMN = "disambig_assignee_organization"  # PatentsView bulk-file default
_SEARCH_DEBOUNCE_MS = 250
_MAX_CANONICALS = 5000  # cap the visible canonical list so a 75k-entry memory stays responsive
_ENTITY_TYPES = ("company", "individual", "unknown")


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
        self._thread: QThread | None = None  # reference-seeding worker (multi-GB file scans)
        self._worker: CallWorker | None = None

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
        self._canon_list.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection  # multi-select for bulk "Set type…"
        )
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
        col.addLayout(
            self._button_row(
                ("Tag all…", self._tag_all),
                ("Set type…", self._set_type),
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

        # Review queue: narrow the table to aliases learned from marginal fuzzy matches.
        self._review_only = QCheckBox("Only aliases learned below")
        self._review_cap = QSpinBox()
        self._review_cap.setRange(1, 100)
        self._review_cap.setValue(95)
        self._review_only.toggled.connect(self._apply_review_filter)
        self._review_cap.valueChanged.connect(self._apply_review_filter)
        review_row = QHBoxLayout()
        review_row.addWidget(self._review_only)
        review_row.addWidget(self._review_cap)
        review_row.addStretch(1)
        col.addLayout(review_row)

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
        col.addLayout(
            self._button_row(
                ("Mark reviewed", self._confirm_alias), ("Delete alias", self._delete_alias)
            )
        )
        return tab

    # -- file + save/cancel rows -------------------------------------------
    def _build_file_row(self) -> QHBoxLayout:
        return self._button_row(
            ("Import…", self._import),
            ("Seed from reference…", self._seed_from_reference),
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

    def _apply_review_filter(self) -> None:
        cap = self._review_cap.value() if self._review_only.isChecked() else None
        self._alias_model.set_review_filter(cap)
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
        for name in shown:  # show the stored type inline; keep the raw name in the item's data
            entity_type = self._memory.entity_type(name)
            item = QListWidgetItem(f"{name}  ·  {entity_type}" if entity_type else name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self._canon_list.addItem(item)
        extra = len(matches) - len(shown)
        self._canon_note.setText(
            f"showing {len(shown):,} of {len(matches):,} matches (+{extra:,} more)"
            if extra > 0
            else f"{len(shown):,} match(es)"
        )

    def _selected_canonical(self) -> str | None:
        item = self._canon_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _selected_canonicals(self) -> list[str]:
        return [item.data(Qt.ItemDataRole.UserRole) for item in self._canon_list.selectedItems()]

    # -- canonical edits ---------------------------------------------------
    def _add_canonical(self) -> None:
        if self._busy():
            return
        name, ok = QInputDialog.getText(self, "Add canonical", "Canonical name:")
        if ok and name.strip():
            self._memory.add_canonical(name.strip())
            self._refresh()

    def _rename_canonical(self) -> None:
        if self._busy():
            return
        old = self._selected_canonical()
        if old is None:
            return
        new, ok = QInputDialog.getText(self, "Rename canonical", "New name:", text=old)
        if ok and new.strip():
            self._memory.rename_canonical(old, new.strip())
            self._refresh()

    def _merge_canonical(self) -> None:
        if self._busy():
            return
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
        if self._busy():
            return
        name = self._selected_canonical()
        if name is not None:
            self._memory.delete_canonical(name)
            self._refresh()

    # -- entity-type tagging -----------------------------------------------
    def _tag_all(self) -> None:
        """Classify every canonical (Rules or ML) off the GUI thread, tagging the working memory."""
        if self._busy():
            return
        pp_installed = importlib.util.find_spec("probablepeople") is not None
        pp_label = "ML (probablepeople)" if pp_installed else "ML (probablepeople — not installed)"
        model_label = "ML (built-in, no setup)"
        choice, ok = QInputDialog.getItem(
            self,
            "Tag all entities",
            "Classify every canonical name as company / individual / unknown using:",
            ["Rules (fast, no dependency)", model_label, pp_label],
            0,
            False,
        )
        if not ok:
            return
        method = (
            "model"
            if choice == model_label
            else "probablepeople"
            if choice == pp_label
            else "rules"
        )
        memory = self._memory
        self._counts.setText(f"Tagging {memory.counts()[0]:,} entities ({method})…")
        thread = QThread(self)
        worker = CallWorker(lambda: tag_memory(memory, method=method))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_tag_ready)
        worker.failed.connect(self._on_reference_failed)  # reuse: refresh + warn on failure
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._cleanup_thread)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _on_tag_ready(self, result: object) -> None:
        tagged = result if isinstance(result, int) else 0
        self._refresh()
        canonicals, aliases = self._memory.counts()
        self._counts.setText(
            f"{canonicals:,} canonical names · {aliases:,} learned aliases · "
            f"tagged {tagged:,} entities (Save to persist)"
        )

    def _set_type(self) -> None:
        """Set the selected canonical(s) to a chosen entity type."""
        if self._busy():
            return
        names = self._selected_canonicals()
        if not names:
            return
        choice, ok = QInputDialog.getItem(
            self,
            "Set entity type",
            f"Set type for {len(names)} selected entity(ies):",
            list(_ENTITY_TYPES),
            0,
            False,
        )
        if ok and choice:
            for name in names:
                self._memory.set_type(name, choice)
            self._refresh()

    def _delete_alias(self) -> None:
        if self._busy():
            return
        rows = sorted({i.row() for i in self._alias_table.selectedIndexes()})
        if rows:
            self._alias_model.delete_aliases(rows)
            self._refresh()

    def _confirm_alias(self) -> None:
        """Accept the selected marginal aliases: score → 100, dropping them off the review queue."""
        if self._busy():
            return
        rows = sorted({i.row() for i in self._alias_table.selectedIndexes()})
        if rows:
            self._alias_model.confirm_aliases(rows)
            self._update_alias_note()

    # -- reference seeding ---------------------------------------------------
    def _seed_from_reference(self) -> None:
        """Build canonicals from a disambiguated assignee reference (TSV/CSV/Parquet).

        Streams the file off the GUI thread (it may be multi-GB) and merges every distinct
        organization name into the working memory as a canonical. Save persists the result.
        """
        if self._busy():
            return
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Disambiguated reference file", "", _REFERENCE_FILTER
        )
        if not path_str:
            return
        column, ok = QInputDialog.getText(
            self,
            "Reference name column",
            "Organization-name column\n"
            f"(PatentsView bulk file: {_REFERENCE_NAME_COLUMN};"
            ' a compact extract: "organization"):',
            text=_REFERENCE_NAME_COLUMN,
        )
        column = column.strip()
        if not ok or not column:
            return
        path = Path(path_str)
        self._counts.setText(f"Seeding from {path.name} — scanning…")
        thread = QThread(self)
        worker = CallWorker(lambda: build_reference(path, column))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_reference_ready)
        worker.failed.connect(self._on_reference_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._cleanup_thread)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _on_reference_ready(self, result: object) -> None:
        if not isinstance(result, ReferenceGazetteer):
            return
        before = self._memory.counts()[0]
        self._memory.merge(result.memory)
        added = self._memory.counts()[0] - before
        self._refresh()
        canonicals, aliases = self._memory.counts()
        self._counts.setText(
            f"{canonicals:,} canonical names · {aliases:,} learned aliases · "
            f"seeded {added:,} new from reference (Save to persist)"
        )

    def _on_reference_failed(self, message: str) -> None:
        self._refresh()  # restore the counts line
        QMessageBox.warning(self, "Seed failed", f"Could not read the reference:\n{message}")

    def _cleanup_thread(self) -> None:
        if self._thread is not None:
            self._thread.deleteLater()
        self._thread = None
        self._worker = None

    def _busy(self) -> bool:
        """True (with a notice) while a background task runs — the worker mutates or reads the
        working memory, so every edit, import, export, and close must wait for it."""
        if self._thread is None:
            return False
        QMessageBox.information(self, "Busy", "Wait for the current background task to finish.")
        return True

    def closeEvent(self, a0: QCloseEvent | None) -> None:
        """Never destroy the dialog while the seeding thread is alive."""
        if self._busy():
            if a0 is not None:
                a0.ignore()
            return
        super().closeEvent(a0)

    def reject(self) -> None:
        """Route Esc/Cancel through the busy guard."""
        if not self._busy():
            super().reject()

    # -- file operations ---------------------------------------------------
    def _import(self) -> None:
        if self._busy():
            return
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
        if self._busy():  # the worker may be mid-mutation — exporting now would snapshot torn state
            return
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
        if self._busy():  # the worker holds the old memory; clearing now silently loses its result
            return
        self._memory = EntityMemory()
        self._alias_model = EntityAliasModel(self._memory)
        self._alias_table.setModel(self._alias_model)
        # The fresh model starts unfiltered — re-apply the search box and review-queue widgets so
        # what the table shows keeps matching what the controls say.
        self._alias_model.set_filter(self._alias_search.text())
        self._apply_review_filter()
        self._refresh()

    def _save(self) -> None:
        if self._busy():  # saving mid-scan would persist without the seeded names
            return
        self._store.save(self._memory)
        self.accept()
