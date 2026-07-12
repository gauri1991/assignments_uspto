"""A ``QTableView`` preconfigured for the Metro data-grid look and large datasets."""

from __future__ import annotations

from PyQt6.QtWidgets import QAbstractItemView, QHeaderView, QTableView, QWidget

_ROW_HEIGHT_PX = 38  # a layout metric (not styling), so it lives here rather than in QSS


class DataTable(QTableView):
    """Full-row-select, grid-less table with 38px rows, tuned for millions of rows.

    ``setUniformRowHeights``-style behaviour is achieved via a fixed default section size so the
    view never measures per-row content — essential when the model wraps a huge Arrow table.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        self.setWordWrap(False)
        self.setSortingEnabled(False)  # header-click sorting is wired up in Phase 4
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

        # PyQt6 types these headers as Optional; on a constructed view they are never None.
        vheader = self.verticalHeader()
        assert vheader is not None
        vheader.setVisible(False)
        vheader.setDefaultSectionSize(_ROW_HEIGHT_PX)  # fixed row height, even while hidden

        hheader = self.horizontalHeader()
        assert hheader is not None
        hheader.setHighlightSections(False)
        hheader.setStretchLastSection(True)
        hheader.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hheader.setDefaultSectionSize(160)
