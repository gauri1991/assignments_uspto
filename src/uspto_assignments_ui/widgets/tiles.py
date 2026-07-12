"""Metro tiles: square 140x140 buttons with a bottom-left caption, laid out on an 8px grid."""

from __future__ import annotations

from typing import Literal

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QGridLayout, QLabel, QPushButton, QVBoxLayout, QWidget

TileVariant = Literal["accent", "neutral", "control"]
_GUTTER_PX = 8
_COLUMNS = 4


class Tile(QPushButton):
    """A 140x140 flat tile styled purely via dynamic properties (``tile`` + ``variant``)."""

    def __init__(
        self, title: str, variant: TileVariant = "neutral", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setProperty("tile", "true")
        self.setProperty("variant", variant)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addStretch(1)
        caption = QLabel(title)
        caption.setProperty("role", "tile")
        caption.setWordWrap(True)
        # Let clicks fall through the caption to the button.
        caption.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(caption, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)


class TileGrid(QWidget):
    """A left/top-aligned grid of tiles with 8px gutters."""

    def __init__(self, columns: int = _COLUMNS, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._columns = columns
        self._count = 0
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(_GUTTER_PX)
        self._grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

    def add_tile(self, tile: Tile) -> None:
        """Append a tile in row-major order."""
        row, column = divmod(self._count, self._columns)
        self._grid.addWidget(tile, row, column)
        self._count += 1

    def clear_tiles(self) -> None:
        """Remove and delete all tiles."""
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._count = 0
