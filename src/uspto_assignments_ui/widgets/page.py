"""Typography components that carry the Metro look Qt QSS cannot express on its own.

Qt style sheets have no ``letter-spacing``, no ``text-transform``, and unreliable numeric
``font-weight``. These three are central to the Metro brief (the light 26px title is "the
signature"; section labels are uppercase with 1px tracking), so they are applied here via
``QFont`` while all colour/size stays in ``metro.qss``.
"""

from __future__ import annotations

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QLabel, QWidget

_SECTION_TRACKING_PX = 1.0


class PageTitle(QLabel):
    """A 26px page title in Segoe UI **Light** (weight 300) — the Metro signature weight."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setProperty("role", "h1")  # size/colour from QSS
        font = self.font()
        font.setWeight(QFont.Weight.Light)  # 300; QSS numeric weight is unreliable in Qt6
        self.setFont(font)


class SectionLabel(QLabel):
    """An 11px/600 uppercase section label with 1px letter-spacing, colour #6D6D6D."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text.upper(), parent)  # uppercase in Python; QSS has no text-transform
        self.setProperty("role", "section")
        font = self.font()
        font.setWeight(QFont.Weight.DemiBold)  # 600
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, _SECTION_TRACKING_PX)
        self.setFont(font)

    def setText(self, a0: str | None) -> None:  # signature matches QLabel; keeps text uppercase
        super().setText(a0.upper() if a0 is not None else a0)
