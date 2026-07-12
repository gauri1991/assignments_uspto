"""Shared test configuration.

Force Qt's offscreen platform so the UI tests run headless (CI, no display). Must be set
before any PyQt6 import, so it lives here in conftest (imported at collection time).
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
