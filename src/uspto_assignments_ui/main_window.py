"""The main application window: a landing page, then filterable/paginated tabbed tables.

Opening a dataset first shows a load template (fields, record cap, page size); parsing then runs
on a background thread with a progress indicator, so the GUI never blocks. Each kept table becomes
a :class:`TablePanel`; export honours the current filter/selection scope.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QObject, QThread
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QInputDialog,
    QMainWindow,
    QProgressBar,
    QStackedWidget,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from uspto_assignments import (
    FORMAT_SUFFIX,
    ExportFormat,
    Query,
    TableStore,
    exporters,
    open_dataset,
    scope_suffix,
    unique_path,
)

from .settings import BatchTemplateStore, EntityMemoryStore, QueryStore, RecentStore
from .widgets.batch_dialog import BatchDialog
from .widgets.entity_dialog import EntityDialog
from .widgets.export_dialog import ExportDialog
from .widgets.landing import LandingPage
from .widgets.load_dialog import LoadDialog, LoadTemplate
from .widgets.page import PageTitle
from .widgets.query_dialog import QueryDialog
from .widgets.save_dialog import SaveDialog
from .widgets.table_panel import TablePanel
from .workers import CallWorker, ParseWorker

_OPEN_FILTER = "USPTO assignment (*.xml *.zip);;All files (*)"
_SAVE_FILTERS: dict[ExportFormat, str] = {
    "parquet": "Parquet (*.parquet)",
    "xlsx": "Excel (*.xlsx)",
    "csv": "CSV (*.csv)",
    "json": "JSON (*.json)",
    "feather": "Feather/Arrow (*.arrow)",
}


class MainWindow(QMainWindow):
    """Top-level window: open (with a load template), filter/sort/paginate, and export."""

    def __init__(self, store: TableStore | None = None) -> None:
        super().__init__()
        self.setWindowTitle("USPTO Assignment Viewer")
        self.resize(1180, 760)

        self._store: TableStore | None = None
        self._source_stem: str = "export"
        self._page_size: int | None = 1000
        self._pending_template: LoadTemplate | None = None
        self._pending_source: Path | None = None
        self._thread: QThread | None = None
        self._worker: QObject | None = None
        self._recent_store = RecentStore()
        self._query_store = QueryStore()
        self._batch_store = BatchTemplateStore()
        self._entity_store = EntityMemoryStore()
        self._batch_dialog: BatchDialog | None = None  # kept alive; shown non-modally (resizable)
        self._entity_dialog: EntityDialog | None = None  # non-modal so both windows coexist

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(24, 20, 24, 12)
        layout.setSpacing(12)
        layout.addWidget(PageTitle("Patent Assignments"))

        self._stack = QStackedWidget()
        self._landing = LandingPage()
        self._landing.open_file_requested.connect(self._choose_file)
        self._landing.open_folder_requested.connect(self._choose_folder)
        self._landing.open_recent_requested.connect(self._open_recent)
        self._landing.clear_recent_requested.connect(self._clear_recent)
        self._tabs = QTabWidget()
        self._stack.addWidget(self._landing)  # index 0
        self._stack.addWidget(self._tabs)  # index 1
        layout.addWidget(self._stack)

        self._progress = QProgressBar()
        self._progress.setMaximumWidth(200)
        self._progress.setVisible(False)
        status = self.statusBar()
        if status is not None:
            status.addPermanentWidget(self._progress)

        self._build_actions()
        self._build_menu()
        self._build_toolbar()
        self._update_actions()
        self._refresh_recent()
        self._set_status("Open a USPTO .xml/.zip or an Arrow/Parquet dataset folder to begin")
        if store is not None:
            self.load_store(store)

    # -- actions / menu / toolbar -----------------------------------------
    def _build_actions(self) -> None:
        self._act_open = self._make_action("&Open XML/ZIP…", self._choose_file, "Ctrl+O")
        self._act_open_ds = self._make_action("Open &dataset folder…", self._choose_folder)
        self._act_save = self._make_action("&Save processed…", self._save_processed, "Ctrl+S")
        self._act_export = self._make_action(
            "&Export current table…", self._export_current, "Ctrl+E"
        )
        self._act_export_all = self._make_action("Export &all tables…", self._export_all)
        self._act_close = self._make_action("&Close dataset", self._close_dataset, "Ctrl+W")
        self._act_exit = self._make_action("E&xit", self.close)
        self._act_save_query = self._make_action("&Save current query…", self._save_query)
        self._act_manage_queries = self._make_action("&Manage queries…", self._manage_queries)
        self._act_batch = self._make_action("&Batch processing…", self._open_batch, "Ctrl+B")
        self._act_entities = self._make_action("&Entity memory…", self._open_entities)
        # Actions that require a loaded dataset.
        self._data_actions = (
            self._act_save,
            self._act_export,
            self._act_export_all,
            self._act_close,
            self._act_save_query,
            self._act_manage_queries,
        )

    def _make_action(self, text: str, slot: Callable[[], object], shortcut: str = "") -> QAction:
        action = QAction(text, self)
        if shortcut:
            action.setShortcut(shortcut)
        action.triggered.connect(lambda _checked=False: slot())
        return action

    def _build_menu(self) -> None:
        menubar = self.menuBar()
        if menubar is None:
            return
        file_menu = menubar.addMenu("&File")
        if file_menu is None:
            return
        file_menu.addAction(self._act_open)
        file_menu.addAction(self._act_open_ds)
        file_menu.addSeparator()
        file_menu.addAction(self._act_save)
        file_menu.addAction(self._act_export)
        file_menu.addAction(self._act_export_all)
        file_menu.addSeparator()
        file_menu.addAction(self._act_close)
        file_menu.addAction(self._act_exit)

        queries_menu = menubar.addMenu("&Queries")
        if queries_menu is not None:
            queries_menu.addAction(self._act_save_query)
            queries_menu.addAction(self._act_manage_queries)

        settings_menu = menubar.addMenu("&Settings")
        if settings_menu is not None:
            settings_menu.addAction(self._act_batch)
            settings_menu.addAction(self._act_entities)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        toolbar.addAction(self._act_open)
        toolbar.addAction(self._act_open_ds)
        toolbar.addSeparator()
        toolbar.addAction(self._act_save)
        toolbar.addAction(self._act_export)
        toolbar.addAction(self._act_export_all)
        toolbar.addSeparator()
        toolbar.addAction(self._act_manage_queries)
        toolbar.addAction(self._act_batch)
        toolbar.addAction(self._act_close)
        self.addToolBar(toolbar)

    def _update_actions(self) -> None:
        has_data = self._store is not None
        for action in self._data_actions:
            action.setEnabled(has_data)

    # -- open / close ------------------------------------------------------
    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open USPTO assignment", "", _OPEN_FILTER)
        if not path:
            return
        dialog = LoadDialog(allow_record_limit=True, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._start_parse(Path(path), dialog.template())

    def _choose_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Open dataset folder (Arrow or Parquet)")
        if not path:
            return
        dialog = LoadDialog(allow_record_limit=False, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._open_dataset_folder(Path(path), dialog.template())

    def _open_dataset_folder(self, path: Path, template: LoadTemplate) -> None:
        try:
            self.load_store(open_dataset(path), template)
        except (FileNotFoundError, OSError) as exc:
            self._set_status(f"Could not open dataset: {exc}")
            return
        self._source_stem = path.name
        self._record_recent(path, "dataset")

    def _open_recent(self, raw_path: str) -> None:
        path = Path(raw_path)
        if not path.exists():
            self._set_status(f"No longer available: {path.name}")
            return
        if path.is_dir():
            self._open_dataset_folder(path, LoadTemplate())
        else:
            self._start_parse(path, LoadTemplate())

    def _clear_recent(self) -> None:
        self._recent_store.clear()
        self._refresh_recent()

    def _refresh_recent(self) -> None:
        self._landing.set_recent(self._recent_store.load())

    def _record_recent(self, path: Path, kind: str) -> None:
        self._recent_store.add(str(path), kind)
        self._refresh_recent()

    def _close_dataset(self) -> None:
        self._store = None
        self._tabs.clear()
        self._stack.setCurrentWidget(self._landing)
        self._update_actions()
        self._set_status("Dataset closed")

    def _save_processed(self) -> None:
        if self._store is None:
            return
        dialog = SaveDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        fmt = dialog.selected_format()
        directory = QFileDialog.getExistingDirectory(self, "Save processed dataset to folder")
        if not directory:
            return
        store = self._store
        out_dir = unique_path(Path(directory) / self._source_stem)  # named after the source
        kind = "Parquet" if fmt == "parquet" else "Arrow"

        def _done(counts: object) -> None:
            n = len(counts) if isinstance(counts, dict) else 0
            self._set_status(f"Saved {n} tables as {kind} → {out_dir.name}")

        self._run_task(
            lambda: exporters.export_store(store, out_dir, fmt),
            busy=f"Saving as {kind}…",
            done=_done,
        )

    def _start_parse(self, source: Path, template: LoadTemplate) -> None:
        if self._thread is not None:
            self._set_status("Busy — wait for the current task to finish")
            return
        self._pending_template = template
        self._pending_source = source
        store_dir = Path(tempfile.mkdtemp(prefix="uspto_store_"))
        worker = ParseWorker(source, store_dir, limit=template.max_records)
        worker.progress.connect(self._on_parse_progress)
        self._show_busy(f"Parsing {source.name}…")
        self._spawn(worker, self._on_parse_finished, self._on_parse_failed)

    def _on_parse_progress(self, count: int) -> None:
        self._set_status(f"Parsing… {count:,} assignments")

    def _on_parse_finished(self, store: object) -> None:
        self._hide_busy()
        if isinstance(store, TableStore):
            self.load_store(store, self._pending_template)
            if self._pending_source is not None:
                self._source_stem = self._pending_source.stem
                self._record_recent(self._pending_source, "file")

    def _on_parse_failed(self, message: str) -> None:
        self._hide_busy()
        self._set_status(f"Parse failed: {message}")

    # -- export ------------------------------------------------------------
    def _export_current(self) -> None:
        panel = self.current_panel()
        if panel is None:
            self._set_status("Nothing to export — open a dataset first")
            return
        view_rows = panel.current_view_rows()
        selected = panel.selected_source_rows()
        dialog = ExportDialog(
            total_rows=panel.table.num_rows,
            view_rows=len(view_rows),
            selected_rows=len(selected),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        fmt = dialog.selected_format()
        scope = dialog.selected_scope()
        rows = None if scope == "all" else (view_rows if scope == "filtered" else selected)

        name = self._current_table_name()
        default = f"{self._source_stem}_{name}{scope_suffix(scope)}{FORMAT_SUFFIX[fmt]}"
        chosen, _ = QFileDialog.getSaveFileName(
            self,
            "Export table",
            default,
            _SAVE_FILTERS[fmt],
            "",
            QFileDialog.Option.DontConfirmOverwrite,
        )
        if not chosen:
            return
        table = panel.table
        target = unique_path(Path(chosen))  # never overwrite: auto-rename Windows-style
        self._run_task(
            lambda: exporters.export(table, target, fmt, rows=rows, sheet_name=name),
            busy=f"Exporting {name}…",
            done=lambda n: self._set_status(f"Exported {n:,} rows → {target.name}"),
        )

    def _export_all(self) -> None:
        if self._store is None:
            self._set_status("Nothing to export — open a dataset first")
            return
        dialog = ExportDialog(show_scope=False, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        fmt = dialog.selected_format()
        directory = QFileDialog.getExistingDirectory(self, "Export all tables to folder")
        if not directory:
            return
        store = self._store
        out_dir = unique_path(Path(directory) / self._source_stem)  # folder named after the source

        def _done(counts: object) -> None:
            n = len(counts) if isinstance(counts, dict) else 0
            self._set_status(f"Exported {n} tables → {out_dir.name}")

        self._run_task(
            lambda: exporters.export_store(store, out_dir, fmt),
            busy="Exporting all tables…",
            done=_done,
        )

    # -- threading ---------------------------------------------------------
    def _run_task(
        self, task: Callable[[], object], *, busy: str, done: Callable[[object], None]
    ) -> None:
        if self._thread is not None:
            self._set_status("Busy — wait for the current task to finish")
            return
        worker = CallWorker(task)
        self._show_busy(busy)

        def _finished(result: object) -> None:
            self._hide_busy()
            done(result)

        def _failed(message: str) -> None:
            self._hide_busy()
            self._set_status(f"Failed: {message}")

        self._spawn(worker, _finished, _failed)

    def _spawn(
        self,
        worker: ParseWorker | CallWorker,
        on_finished: Callable[[object], None],
        on_failed: Callable[[str], None],
    ) -> None:
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(on_finished)
        worker.failed.connect(on_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._cleanup_thread)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _cleanup_thread(self) -> None:
        if self._thread is not None:
            self._thread.deleteLater()
        self._thread = None
        self._worker = None

    # -- data --------------------------------------------------------------
    def load_store(self, store: TableStore, template: LoadTemplate | None = None) -> None:
        """Project ``store`` per the template, then show one paginated tab per kept table."""
        if template is not None and template.columns:
            store = store.select_columns(template.columns)
        self._store = store
        self._page_size = template.page_size if template is not None else self._page_size
        self._tabs.clear()
        for name in store.names:
            table = store.table(name)
            panel = TablePanel(table, page_size=self._page_size)
            self._tabs.addTab(panel, f"{name}  ({table.num_rows:,})")
        total = sum(store.row_counts().values())
        self._stack.setCurrentWidget(self._tabs)
        self._update_actions()
        self._set_status(f"Loaded {len(store.names)} tables · {total:,} rows")

    def current_panel(self) -> TablePanel | None:
        """The panel on the active tab, or None if no dataset is loaded."""
        widget = self._tabs.currentWidget()
        return widget if isinstance(widget, TablePanel) else None

    def _panel_for_table(self, name: str) -> TablePanel | None:
        if self._store is None or name not in self._store.names:
            return None
        widget = self._tabs.widget(self._store.names.index(name))
        return widget if isinstance(widget, TablePanel) else None

    def _current_table_name(self) -> str:
        index = self._tabs.currentIndex()
        if self._store is not None and 0 <= index < len(self._store.names):
            return self._store.names[index]
        return "table"

    # -- saved queries -----------------------------------------------------
    def _save_query(self) -> None:
        panel = self.current_panel()
        if panel is None:
            return
        name, ok = QInputDialog.getText(self, "Save query", "Query name:")
        if not ok or not name.strip():
            return
        self._query_store.add(panel.to_query(name.strip(), self._current_table_name()))
        self._set_status(f"Saved query '{name.strip()}'")

    def _manage_queries(self) -> None:
        if self._store is None:
            return
        dialog = QueryDialog(self._query_store, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        query = dialog.selected_query()
        if query is not None:
            self._apply_query(query)

    def _open_batch(self) -> None:
        # Non-modal so it behaves like a normal window (minimize/maximize work on Windows + Linux);
        # reuse a single instance and just re-raise it if it's already open.
        if self._batch_dialog is None:
            self._batch_dialog = BatchDialog(self._batch_store, self._entity_store, parent=self)
        self._batch_dialog.show()
        self._batch_dialog.raise_()
        self._batch_dialog.activateWindow()

    def _open_entities(self) -> None:
        # Non-modal so it can sit alongside the batch window and minimize/maximize. If it's already
        # open, just re-raise it; otherwise create a fresh one (re-reading the memory from disk, so
        # Cancel truly discards edits).
        if self._entity_dialog is not None and self._entity_dialog.isVisible():
            self._entity_dialog.raise_()
            self._entity_dialog.activateWindow()
            return
        self._entity_dialog = EntityDialog(self._entity_store, parent=self)
        self._entity_dialog.show()
        self._entity_dialog.raise_()
        self._entity_dialog.activateWindow()

    def _apply_query(self, query: Query) -> None:
        panel = self._panel_for_table(query.table)
        if panel is None:
            self._set_status(
                f"Query '{query.name}' targets table '{query.table}', which isn't loaded"
            )
            return
        self._tabs.setCurrentWidget(panel)
        panel.apply_query(query)
        self._set_status(f"Applied query '{query.name}'")

    @property
    def tab_widget(self) -> QTabWidget:
        """The central tab widget (one tab per kept table)."""
        return self._tabs

    # -- status / progress -------------------------------------------------
    def _show_busy(self, message: str) -> None:
        self._progress.setRange(0, 0)  # indeterminate: totals are unknown mid-stream
        self._progress.setVisible(True)
        self._set_status(message)

    def _hide_busy(self) -> None:
        self._progress.setVisible(False)

    def _set_status(self, message: str) -> None:
        status = self.statusBar()  # PyQt6 types this Optional; it is created on demand
        if status is not None:
            status.showMessage(message)
