"""QApplication bootstrap: create the app and apply the single Metro stylesheet."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PyQt6.QtWidgets import QApplication

_QSS_PATH = Path(__file__).parent / "resources" / "metro.qss"


def load_stylesheet() -> str:
    """Return the Metro QSS text, with ``@RESOURCES@`` resolved to the resources dir.

    Lets the stylesheet reference bundled assets (e.g. the checkbox tick) by absolute path —
    QSS ``image: url(...)`` needs a resolvable path, and the resources dir isn't the CWD.
    """
    return _QSS_PATH.read_text(encoding="utf-8").replace("@RESOURCES@", _QSS_PATH.parent.as_posix())


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
