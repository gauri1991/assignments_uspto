"""Output-path helpers: non-clobbering names and scope suffixes.

Exports should be named after the source and never overwrite an existing file — instead they get a
Windows-Explorer-style `` (n)`` counter. These helpers are pure so they are trivially testable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

Scope = Literal["all", "filtered", "selected"]

# Row scope -> filename suffix (empty for a full export).
_SCOPE_SUFFIX: dict[Scope, str] = {"all": "", "filtered": "_filtered", "selected": "_selected"}


def scope_suffix(scope: Scope) -> str:
    """Return the filename suffix for an export scope (e.g. ``"_filtered"``)."""
    return _SCOPE_SUFFIX[scope]


def unique_path(path: Path) -> Path:
    """Return ``path`` if free, else the same name with a `` (n)`` counter before the suffix.

    Works for both files (``report.csv`` → ``report (1).csv``) and directories
    (``ad20260709`` → ``ad20260709 (1)``). The returned path is guaranteed not to exist at call
    time (subject to the usual race conditions).
    """
    if not path.exists():
        return path
    parent = path.parent
    # ``.arrow``/``.csv`` -> suffix ".csv"; a dir or extensionless name -> "".
    suffix = path.suffix
    stem = path.name[: -len(suffix)] if suffix else path.name
    counter = 1
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1
