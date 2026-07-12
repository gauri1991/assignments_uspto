"""PyQt6 desktop UI for the USPTO assignment toolkit (Metro / Modern UI style).

This package depends on :mod:`uspto_assignments` (the Qt-free core) and on PyQt6. Import it only
when a desktop environment and the ``ui`` extra are available:

    pip install -e ".[ui]"
    uspto-assign-ui  [PATH]
"""

from __future__ import annotations

from .app import create_app, load_stylesheet
from .main_window import MainWindow

__all__ = ["MainWindow", "create_app", "load_stylesheet"]
