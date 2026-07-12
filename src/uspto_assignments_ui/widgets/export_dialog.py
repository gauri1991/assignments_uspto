"""A dialog to choose an export format and scope (all / filtered view / selected rows)."""

from __future__ import annotations

from typing import Literal

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

ExportScope = Literal["all", "filtered", "selected"]

# Display label -> export format, in menu order.
_FORMATS: list[tuple[str, ExportFormat]] = [
    ("Parquet", "parquet"),
    ("Excel (.xlsx)", "xlsx"),
    ("CSV", "csv"),
    ("JSON", "json"),
    ("Feather (Arrow)", "feather"),
]


class ExportDialog(QDialog):
    """Pick an export format and which rows to export for the current table."""

    def __init__(
        self,
        *,
        total_rows: int = 0,
        view_rows: int = 0,
        selected_rows: int = 0,
        show_scope: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export")
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        layout.addWidget(SectionLabel("Export"))

        form = QFormLayout()
        form.setSpacing(10)

        self._format = QComboBox()
        for label, fmt in _FORMATS:
            self._format.addItem(label, fmt)
        form.addRow("Format", self._format)

        self._scope: QComboBox | None = None
        if show_scope:
            self._scope = QComboBox()
            self._scope.addItem(f"All rows ({total_rows:,})", "all")
            self._scope.addItem(f"Filtered view ({view_rows:,})", "filtered")
            if selected_rows > 0:
                self._scope.addItem(f"Selected rows ({selected_rows:,})", "selected")
            self._scope.setCurrentIndex(self._scope.count() - 1)  # most specific meaningful scope
            form.addRow("Scope", self._scope)
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
        """The chosen export format."""
        return self._format.currentData()

    def selected_scope(self) -> ExportScope:
        """The chosen row scope: ``all``, ``filtered``, or ``selected`` (``all`` if hidden)."""
        return self._scope.currentData() if self._scope is not None else "all"
