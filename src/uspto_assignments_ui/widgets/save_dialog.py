"""A small dialog to choose the on-disk format when saving a processed dataset."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QVBoxLayout,
    QWidget,
)

from uspto_assignments import ExportFormat

from .page import SectionLabel

# Display label -> export format used to persist a reopenable dataset.
_FORMATS: list[tuple[str, ExportFormat]] = [
    ("Parquet (portable)", "parquet"),
    ("Arrow / Feather (fastest reopen)", "feather"),
]


class SaveDialog(QDialog):
    """Pick Parquet or Arrow for a processed dataset that can be reopened directly."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Save processed dataset")
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        layout.addWidget(SectionLabel("Save processed dataset"))

        form = QFormLayout()
        form.setSpacing(10)
        self._format = QComboBox()
        for label, fmt in _FORMATS:
            self._format.addItem(label, fmt)
        form.addRow("Format", self._format)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            ok_button.setProperty("primary", "true")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_format(self) -> ExportFormat:
        """The chosen persistence format (``parquet`` or ``feather``)."""
        return self._format.currentData()
