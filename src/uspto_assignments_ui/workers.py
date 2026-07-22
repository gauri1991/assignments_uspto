"""Background workers so long-running core calls never block the GUI thread.

A ``QObject`` worker is moved onto a ``QThread`` and communicates only via signals — the Qt-safe
way to do work off the UI thread. Parsing a multi-GB file takes minutes, so it must run here with
progress reporting; the window stays responsive and shows a progress indicator.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from uspto_assignments import (
    BatchEvent,
    BatchTemplate,
    CpcRunContext,
    EntityMemory,
    ExistingPolicy,
    ExportFormat,
    parse_to_store,
    run_batch,
)


class ParseWorker(QObject):
    """Parse a USPTO ``.xml``/``.zip`` into a memory-mapped store, off the UI thread.

    Signals:
        progress: Emitted with the running record count as parsing proceeds.
        finished: Emitted with the resulting :class:`TableStore` on success.
        failed: Emitted with a human-readable message on any error.
    """

    progress = pyqtSignal(int)
    finished = pyqtSignal(object)  # TableStore
    failed = pyqtSignal(str)

    def __init__(self, source: Path, store_dir: Path, *, limit: int | None = None) -> None:
        super().__init__()
        self._source = source
        self._store_dir = store_dir
        self._limit = limit

    def run(self) -> None:
        """Do the parse; emit ``finished`` or ``failed``. Runs on the worker thread."""
        try:
            store = parse_to_store(
                self._source, self._store_dir, limit=self._limit, progress=self.progress.emit
            )
        except Exception as exc:  # thread boundary: report any error via the failed signal
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(store)


class CallWorker(QObject):
    """Run an arbitrary no-argument callable off the UI thread (e.g. an export).

    Signals:
        finished: Emitted with the callable's return value on success.
        failed: Emitted with a human-readable message on any error.
    """

    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, task: Callable[[], object]) -> None:
        super().__init__()
        self._task = task

    def run(self) -> None:
        """Invoke the task; emit ``finished`` or ``failed``. Runs on the worker thread."""
        try:
            result = self._task()
        except Exception as exc:  # thread boundary: report any error via the failed signal
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(result)


class BatchWorker(QObject):
    """Run a batch template over many inputs off the UI thread, streaming progress events.

    Signals:
        event: Emitted with each :class:`~uspto_assignments.BatchEvent` as processing proceeds.
        finished: Emitted with the :class:`~uspto_assignments.BatchResult` on completion.
        failed: Emitted with a message if the run itself errors (individual file errors are
            reported as events and do not stop the batch).
    """

    # Named ``batch_event`` (not ``event``) to avoid clashing with ``QObject.event``.
    batch_event = pyqtSignal(object)  # BatchEvent
    finished = pyqtSignal(object)  # BatchResult
    failed = pyqtSignal(str)

    def __init__(  # noqa: PLR0913 - a Qt worker bundling the run's parameters
        self,
        template: BatchTemplate,
        inputs: list[Path],
        out_root: Path,
        *,
        workers: int,
        timestamp: str,
        memory: EntityMemory | None = None,
        cpc_ctx: CpcRunContext | None = None,
        trace_steps: bool = False,
        trace_fmt: ExportFormat = "parquet",
        flat_output: bool = False,
        existing: ExistingPolicy = "overwrite",
        mirror_tree: bool = False,
    ) -> None:
        super().__init__()
        self._template = template
        self._inputs = inputs
        self._out_root = out_root
        self._workers = workers
        self._timestamp = timestamp
        self._memory = memory
        self._cpc_ctx = cpc_ctx
        self._trace_steps = trace_steps
        self._trace_fmt: ExportFormat = trace_fmt
        self._flat_output = flat_output
        self._existing: ExistingPolicy = existing
        self._mirror_tree = mirror_tree
        self._stop = threading.Event()

    def cancel(self) -> None:
        """Request cooperative cancellation (takes effect between files). GUI-thread safe."""
        self._stop.set()

    def run(self) -> None:
        """Run the batch; emit ``finished`` or ``failed``. Runs on the worker thread."""
        try:
            result = run_batch(
                self._template,
                self._inputs,
                self._out_root,
                workers=self._workers,
                timestamp=self._timestamp,
                memory=self._memory,
                on_event=self._emit_event,
                cpc_ctx=self._cpc_ctx,
                should_stop=self._stop.is_set,
                trace_steps=self._trace_steps,
                trace_fmt=self._trace_fmt,
                flat_output=self._flat_output,
                existing=self._existing,
                mirror_tree=self._mirror_tree,
            )
        except Exception as exc:  # thread boundary: report any error via the failed signal
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(result)

    def _emit_event(self, event: BatchEvent) -> None:
        self.batch_event.emit(event)


class LogEmitter(QObject):
    """Bridges Python ``logging`` records to a Qt signal (thread-safe via queued connections)."""

    message = pyqtSignal(str)


class QtLogHandler(logging.Handler):
    """A ``logging.Handler`` that forwards formatted records through a :class:`LogEmitter`."""

    def __init__(self, emitter: LogEmitter) -> None:
        super().__init__()
        self._emitter = emitter

    def emit(self, record: logging.LogRecord) -> None:
        self._emitter.message.emit(self.format(record))
