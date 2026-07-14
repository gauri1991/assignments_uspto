"""The landing page shown when no dataset is loaded: get-started tiles + recent files."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from ..settings import RecentEntry
from .page import SectionLabel
from .tiles import Tile, TileGrid


class LandingPage(QWidget):
    """Get-started tiles plus a recent-files row; emits signals for each open action."""

    open_file_requested = pyqtSignal()
    open_folder_requested = pyqtSignal()
    open_table_requested = pyqtSignal()  # a single Parquet/Arrow/Feather/CSV file to view
    open_recent_requested = pyqtSignal(str)  # the recent path
    clear_recent_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(SectionLabel("Get started"))

        grid = TileGrid()
        open_xml = Tile("Open\nXML / ZIP", variant="accent")
        open_xml.clicked.connect(lambda _checked=False: self.open_file_requested.emit())
        open_folder = Tile("Open\ndataset\nfolder", variant="neutral")
        open_folder.clicked.connect(lambda _checked=False: self.open_folder_requested.emit())
        open_table = Tile("View\nParquet /\ndata file", variant="neutral")
        open_table.clicked.connect(lambda _checked=False: self.open_table_requested.emit())
        grid.add_tile(open_xml)
        grid.add_tile(open_folder)
        grid.add_tile(open_table)
        layout.addWidget(grid)

        layout.addWidget(
            SectionLabel("Tip · choose fields, a record limit, and page size when opening")
        )

        # Recent section (hidden until there is history).
        self._recent_header = SectionLabel("Recent")
        layout.addWidget(self._recent_header)
        self._recent_grid = TileGrid()
        layout.addWidget(self._recent_grid)

        layout.addStretch(1)
        self.set_recent([])

    def set_recent(self, entries: Sequence[RecentEntry]) -> None:
        """Rebuild the recent-files tiles (hides the section when empty)."""
        self._recent_grid.clear_tiles()
        for entry in entries:
            tile = Tile(Path(entry.path).name, variant="neutral")
            tile.clicked.connect(
                lambda _checked=False, path=entry.path: self.open_recent_requested.emit(path)
            )
            self._recent_grid.add_tile(tile)
        if entries:
            clear_tile = Tile("Clear\nrecent", variant="control")
            clear_tile.clicked.connect(lambda _checked=False: self.clear_recent_requested.emit())
            self._recent_grid.add_tile(clear_tile)
        self._recent_header.setVisible(bool(entries))
        self._recent_grid.setVisible(bool(entries))
