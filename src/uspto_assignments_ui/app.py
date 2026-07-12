"""QApplication bootstrap: create the app and apply the single Metro stylesheet."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PyQt6.QtWidgets import QApplication

_QSS_PATH = Path(__file__).parent / "resources" / "metro.qss"


def load_stylesheet() -> str:
    """Return the Metro QSS text."""
    return _QSS_PATH.read_text(encoding="utf-8")


def create_app(argv: Sequence[str] | None = None) -> QApplication:
    """Create the QApplication and apply ``metro.qss`` via ``setStyleSheet``.

    Reuses an existing ``QApplication`` instance if one already exists (e.g. under pytest-qt).
    """
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication(list(argv) if argv is not None else [])
    app.setApplicationName("USPTO Assignment Viewer")
    app.setStyleSheet(load_stylesheet())
    return app
