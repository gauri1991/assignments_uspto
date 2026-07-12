"""Initial-load template: choose fields, a record limit, and a page size before opening data."""

from __future__ import annotations

from dataclasses import dataclass, field

from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .field_tree import FieldTree
from .page import SectionLabel

# Page-size label -> rows per page (None = show all rows on one page).
_PAGE_SIZES: list[tuple[str, int | None]] = [
    ("100 / page", 100),
    ("500 / page", 500),
    ("1,000 / page", 1000),
    ("5,000 / page", 5000),
    ("All rows", None),
]
_MAX_RECORDS_CAP = 100_000_000
_DEFAULT_PAGE_INDEX = 2  # 1,000 / page


@dataclass(frozen=True)
class LoadTemplate:
    """User choices for an initial load: record cap, page size, and per-table columns."""

    max_records: int | None = None
    page_size: int | None = 1000
    # table name -> selected columns; empty list drops the table, missing key keeps all columns.
    columns: dict[str, list[str]] = field(default_factory=dict)


class LoadDialog(QDialog):
    """Pick fields, a maximum record count, and a page size before loading a dataset."""

    def __init__(self, *, allow_record_limit: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Load options")
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        layout.addWidget(SectionLabel("Load options"))

        form = QFormLayout()
        form.setSpacing(10)

        self._max = QSpinBox()
        self._max.setRange(0, _MAX_RECORDS_CAP)
        self._max.setSpecialValueText("All records")  # shown when value == 0
        self._max.setValue(0)
        self._max.setEnabled(allow_record_limit)
        form.addRow("Max records", self._max)

        self._page = QComboBox()
        for label, size in _PAGE_SIZES:
            self._page.addItem(label, size)
        self._page.setCurrentIndex(_DEFAULT_PAGE_INDEX)
        form.addRow("Page size", self._page)
        layout.addLayout(form)

        layout.addWidget(SectionLabel("Fields"))
        self._fields = FieldTree()
        layout.addWidget(self._fields)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            ok_button.setProperty("primary", "true")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def template(self) -> LoadTemplate:
        """Return the chosen load template."""
        return LoadTemplate(
            max_records=self._max.value() or None,
            page_size=self._page.currentData(),
            columns=self._fields.selected_columns(),
        )
