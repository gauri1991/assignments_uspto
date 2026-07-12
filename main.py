"""Thin CLI shim — the implementation lives in the ``uspto_assignments`` package.

Kept so ``python main.py ...`` keeps working; new code should import the package directly
(``from uspto_assignments import ...``) or use the ``uspto-assign`` console script.
"""

from __future__ import annotations

from uspto_assignments.cli import main

if __name__ == "__main__":
    main()
