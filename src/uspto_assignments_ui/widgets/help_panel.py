"""A contextual help panel: a crisp explanation of the selected template or step.

Toggled by the batch dialog's Help button; content updates as the user selects a saved
template or a step in the steps list. Rendering only — the actual copy lives in
:mod:`uspto_assignments_ui.help_content` so it stays testable without a QApplication.
"""

from __future__ import annotations

from collections.abc import Sequence

from PyQt6.QtWidgets import QTextBrowser, QVBoxLayout, QWidget

from uspto_assignments import BatchStep

from ..help_content import step_help_html, template_help_html, welcome_help_html
from .page import SectionLabel


class HelpPanel(QWidget):
    """Shows the help text for whatever is currently selected in the batch dialog."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # A narrow column wraps every long word onto its own line (unreadable) — hold a
        # sane minimum so the other two columns give up space instead, however the batch
        # dialog is currently sized.
        self.setMinimumWidth(280)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(SectionLabel("Help"))
        self._body = QTextBrowser()
        self._body.setOpenExternalLinks(False)
        layout.addWidget(self._body, 1)
        self.show_welcome()

    def show_step(self, step: BatchStep) -> None:
        """Show help for one step (the steps-list selection takes priority over a template)."""
        self._body.setHtml(step_help_html(step))

    def show_template(self, name: str, steps: Sequence[BatchStep], description: str = "") -> None:
        """Show help for a template as a whole (no step currently selected).

        ``description`` is the template's own embedded help (if any); it takes precedence over
        the built-in curated help (see ``help_content.template_help_html``).
        """
        self._body.setHtml(template_help_html(name, steps, description))

    def show_welcome(self) -> None:
        """Show the default "how this panel works" text (nothing selected yet)."""
        self._body.setHtml(welcome_help_html())
