"""Convenience launcher for the desktop UI: ``python run_ui.py [PATH]``.

Equivalent to the ``uspto-assign-ui`` console script; requires the ``ui`` extra (PyQt6).
"""

from __future__ import annotations

from uspto_assignments_ui.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
