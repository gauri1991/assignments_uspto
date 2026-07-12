"""A read-only preview of a batch pipeline run on a small sample (Power-Query-style dry run).

Shows a per-step summary (rows in→out, columns added) and a tab per resulting table so you can see
"the data as of each step" before committing to a full run.
"""

from __future__ import annotations

import pyarrow as pa
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from uspto_assignments import StepStat

from ..models import ArrowTableModel
from .data_table import DataTable
from .page import SectionLabel

_PREVIEW_ROWS = 200  # rows shown per table in the preview grid


class PreviewDialog(QDialog):
    """Display sample tables + per-step stats from :func:`uspto_assignments.run_preview`."""

    def __init__(
        self,
        tables: dict[str, pa.Table],
        stats: list[StepStat],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pipeline preview")
        self.resize(920, 660)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(SectionLabel("Preview — pipeline run on a sample"))

        summary = QListWidget()
        summary.setProperty("panel", "true")
        summary.setMaximumHeight(180)
        for stat in stats:
            added = f" · +[{', '.join(stat.columns_added)}]" if stat.columns_added else ""
            note = f" · {stat.note}" if stat.note else ""
            summary.addItem(
                f"{stat.index}. {stat.label}: "
                f"{stat.rows_before:,} → {stat.rows_after:,} rows{added}{note}"
            )
        layout.addWidget(summary)

        tabs = QTabWidget()
        for name, table in tables.items():
            tabs.addTab(self._table_tab(table), f"{name}  ({table.num_rows:,})")
        layout.addWidget(tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    @staticmethod
    def _table_tab(table: pa.Table) -> QWidget:
        tab = QWidget()
        column = QVBoxLayout(tab)
        column.setContentsMargins(0, 8, 0, 0)
        column.setSpacing(6)
        header = QLabel(
            f"{table.num_rows:,} rows × {table.num_columns} cols (showing up to {_PREVIEW_ROWS:,})"
        )
        header.setProperty("role", "hint")
        column.addWidget(header)
        view = DataTable()
        view.setModel(ArrowTableModel(table.slice(0, _PREVIEW_ROWS)))
        column.addWidget(view, 1)
        return tab
