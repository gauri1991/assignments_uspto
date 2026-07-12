"""Launch the USPTO Assignment Viewer.

Usage:
    uspto-assign-ui [PATH]

PATH is optional and may be a USPTO ``.xml``/``.zip`` file or a directory holding a previously
written Arrow-IPC store; when given, it is loaded on startup. (Phase 3 loads synchronously; a
threaded parse with a progress bar arrives in Phase 4.)
"""

from __future__ import annotations

import sys
from pathlib import Path

from uspto_assignments import TableStore, open_dataset, parse_to_store

from .app import create_app
from .main_window import MainWindow


def _load_path(path: Path) -> TableStore:
    if path.is_dir():
        return open_dataset(path)  # Arrow or Parquet dataset folder
    return parse_to_store(path, path.with_suffix(path.suffix + ".store"))


def main(argv: list[str] | None = None) -> int:
    """Create the app, show the main window, and run the event loop."""
    args = list(sys.argv[1:] if argv is None else argv)
    app = create_app(sys.argv)
    window = MainWindow()
    if args and Path(args[0]).exists():
        window.load_store(_load_path(Path(args[0])))
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
