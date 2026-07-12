"""Pagination controls: page-size selector, prev/next navigation, and a range label."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QWidget

# Page-size label -> rows per page (None = one page with all rows).
_PAGE_SIZES: list[tuple[str, int | None]] = [
    ("100", 100),
    ("500", 500),
    ("1,000", 1000),
    ("5,000", 5000),
    ("All", None),
]
_DEFAULT_PAGE_SIZE = 1000


class Pager(QWidget):
    """Stateful pager over a total row count; emits ``changed`` when the visible page shifts."""

    changed = pyqtSignal()

    def __init__(self, page_size: int | None = _DEFAULT_PAGE_SIZE, parent: QWidget | None = None):
        super().__init__(parent)
        self._page = 0
        self._total = 0
        self._page_size = page_size

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self._label = QLabel()
        self._label.setProperty("role", "hint")
        self._size = QComboBox()
        for label, size in _PAGE_SIZES:
            self._size.addItem(label, size)
        self._size.setCurrentIndex(
            next((i for i, (_, s) in enumerate(_PAGE_SIZES) if s == page_size), 2)
        )
        self._size.currentIndexChanged.connect(self._on_size_changed)

        self._first = QPushButton("First")
        self._prev = QPushButton("Prev")
        self._next = QPushButton("Next")
        self._last = QPushButton("Last")
        self._first.clicked.connect(lambda: self._go(0))
        self._prev.clicked.connect(lambda: self._go(self._page - 1))
        self._next.clicked.connect(lambda: self._go(self._page + 1))
        self._last.clicked.connect(lambda: self._go(self._pages() - 1))

        row.addWidget(self._label)
        row.addStretch(1)
        row.addWidget(QLabel("Rows/page"))
        row.addWidget(self._size)
        for button in (self._first, self._prev, self._next, self._last):
            row.addWidget(button)

    # -- state --------------------------------------------------------------
    def set_total(self, total: int) -> None:
        """Set the total row count (clamps the current page); does not emit ``changed``."""
        self._total = total
        self._page = min(self._page, self._pages() - 1)
        self._render()

    def bounds(self) -> tuple[int, int]:
        """Return the ``(start, end)`` row offsets of the current page."""
        if self._page_size is None:
            return 0, self._total
        start = self._page * self._page_size
        return start, min(start + self._page_size, self._total)

    def _pages(self) -> int:
        if self._page_size is None or self._total == 0:
            return 1
        return (self._total + self._page_size - 1) // self._page_size

    # -- events -------------------------------------------------------------
    def _on_size_changed(self) -> None:
        self._page_size = self._size.currentData()
        self._page = 0
        self._render()
        self.changed.emit()

    def _go(self, page: int) -> None:
        page = max(0, min(page, self._pages() - 1))
        if page != self._page:
            self._page = page
            self._render()
            self.changed.emit()

    def _render(self) -> None:
        start, end = self.bounds()
        pages = self._pages()
        shown = f"{start + 1:,}–{end:,}" if self._total else "0"
        self._label.setText(
            f"Showing {shown} of {self._total:,}  ·  page {self._page + 1} of {pages}"
        )
        self._first.setEnabled(self._page > 0)
        self._prev.setEnabled(self._page > 0)
        self._next.setEnabled(self._page < pages - 1)
        self._last.setEnabled(self._page < pages - 1)
