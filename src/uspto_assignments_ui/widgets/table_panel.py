"""One tab's UI: a filter bar over a sortable, paginated data table.

The panel owns an :class:`ArrowTableModel` and the current filter/quick-search/sort state. Any
change recomputes the visible row-index array vectorized (``filters.filter_sort``); the
:class:`Pager` then slices that to the current page and swaps it into the model — so even a
4M-row table only ever hands the view one page of indices at a time.
"""

from __future__ import annotations

from typing import Any, cast

import pyarrow as pa
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from uspto_assignments import Query, filters
from uspto_assignments.filters import SortSpec

from ..models.arrow_table_model import ArrowTableModel
from .data_table import DataTable
from .filter_bar import FilterBar
from .page import SectionLabel
from .pager import Pager


class TablePanel(QWidget):
    """Filter bar + sortable, paginated data table + row-count label for one Arrow table."""

    selection_changed = pyqtSignal()

    def __init__(
        self, table: pa.Table, *, page_size: int | None = 1000, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._table = table
        self._model = ArrowTableModel(table)
        self._sort: SortSpec | None = None
        self._full_view: pa.Array[Any] = pa.array(range(table.num_rows), type=pa.int64())

        self._filter_bar = FilterBar(table.column_names, distinct_provider=self._distinct_values)
        self._count = SectionLabel("")
        self._view = DataTable()
        self._view.setModel(self._model)
        self._pager = Pager(page_size)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self._filter_bar)
        layout.addWidget(self._count)
        layout.addWidget(self._view)
        layout.addWidget(self._pager)

        self._filter_bar.changed.connect(self._refresh)
        self._pager.changed.connect(self._apply_page)

        header = self._view.horizontalHeader()
        assert header is not None
        header.setSectionsClickable(True)
        header.sectionClicked.connect(self._on_header_clicked)

        selection = self._view.selectionModel()
        assert selection is not None
        selection.selectionChanged.connect(lambda *_: self.selection_changed.emit())

        self._refresh()

    # -- exposed state for export -----------------------------------------
    @property
    def table(self) -> pa.Table:
        """The underlying (memory-mapped) table."""
        return self._table

    def current_view_rows(self) -> list[int]:
        """All source-table rows matching the current filter/sort (the whole filtered set)."""
        # filter_sort returns a non-null int64 index array, so to_pylist() is list[int].
        return cast("list[int]", self._full_view.to_pylist())

    def selected_source_rows(self) -> list[int]:
        """Source-table row indices of the selected rows on the current page."""
        selection = self._view.selectionModel()
        if selection is None:
            return []
        return [self._model.source_row(index.row()) for index in selection.selectedRows()]

    # -- saved queries -----------------------------------------------------
    def to_query(self, name: str, table_name: str) -> Query:
        """Capture the current filter/sort as a named :class:`Query`."""
        return Query(
            name=name,
            table=table_name,
            combine=self._filter_bar.combine(),
            quick_search=self._filter_bar.quick_search(),
            clauses=self._filter_bar.clauses(),
            sort=self._sort,
        )

    def apply_query(self, query: Query) -> None:
        """Apply a saved query's clauses, combine mode, quick search, and sort."""
        self._sort = query.sort
        self._filter_bar.set_state(
            query.clauses, query.combine, query.quick_search
        )  # triggers refresh

    def _distinct_values(self, column: str) -> list[str] | None:
        return filters.distinct_values(self._table, column)

    # -- internal ---------------------------------------------------------
    def _refresh(self) -> None:
        self._full_view = filters.filter_sort(
            self._table,
            self._filter_bar.clauses(),
            quick_search=self._filter_bar.quick_search(),
            combine=self._filter_bar.combine(),
            sort=self._sort,
        )
        self._pager.set_total(len(self._full_view))
        self._apply_page()
        self._update_count()

    def _apply_page(self) -> None:
        start, end = self._pager.bounds()
        self._model.set_view(self._full_view.slice(start, end - start))

    def _on_header_clicked(self, section: int) -> None:
        column = self._model.columns[section]
        ascending = not (self._sort is not None and self._sort == (column, True))
        self._sort = (column, ascending)
        self._refresh()
        header = self._view.horizontalHeader()
        if header is not None:
            order = Qt.SortOrder.AscendingOrder if ascending else Qt.SortOrder.DescendingOrder
            header.setSortIndicatorShown(True)
            header.setSortIndicator(section, order)

    def _update_count(self) -> None:
        matched = len(self._full_view)
        total = self._table.num_rows
        text = f"{matched:,} rows" if matched == total else f"{matched:,} of {total:,} rows"
        self._count.setText(text)
