"""Settings dialog for the CPC data source, cache, and match defaults.

Edits a :class:`~uspto_assignments.CpcConfig` and persists it to the project config file (via
:class:`~uspto_assignments_ui.settings.CpcConfigStore`). The API key is never entered or stored
here — only the name of the environment variable that holds it.
"""

from __future__ import annotations

import os

from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from uspto_assignments import CpcConfig
from uspto_assignments.cpcconfig import CPC_ENDPOINT_AS_OF

from ..settings import CpcConfigStore
from .page import SectionLabel

_SOURCE_LABELS = [
    ("USPTO ODP / PatentSearch API", "uspto_odp_api"),
    ("Local bulk file", "local_file"),
]
_GRAINS = ["subclass", "main_group", "full_symbol"]
_METRICS = ["shared_count", "jaccard", "rarity_weighted"]


class CpcSettingsDialog(QDialog):
    """Configure and save the project's CPC data source + match defaults."""

    def __init__(  # noqa: PLR0915 - linear widget assembly
        self, store: CpcConfigStore, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._store = store
        config = store.load()
        self.setWindowTitle("CPC data source")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(SectionLabel("CPC data source"))

        form = QFormLayout()
        self._type = QComboBox()
        for label, value in _SOURCE_LABELS:
            self._type.addItem(label, value)
        self._type.setCurrentIndex(max(0, self._type.findData(config.source.type)))
        self._endpoint = QLineEdit(config.source.endpoint)
        self._api_key_env = QLineEdit(config.source.api_key_env)
        self._path = QLineEdit(config.source.path)
        self._path.setPlaceholderText(
            "bulk CPC file (.tsv/.csv/.parquet) for the local-file source"
        )
        self._patent_column = QLineEdit(config.source.patent_column)
        self._code_column = QLineEdit(config.source.code_column)
        self._offline = QComboBox()
        self._offline.addItem("Offline only (never use the network)", True)
        self._offline.addItem("Allow network fetch (per-run opt-in still required)", False)
        self._offline.setCurrentIndex(0 if config.source.offline_only else 1)
        self._batch_size = _int_spin(1, 1000, config.source.batch_size)
        self._rate_limit = _int_spin(1, 600, config.source.rate_limit_per_min)
        self._max_calls = _int_spin(1, 1_000_000, config.source.max_api_calls)

        form.addRow("Source", self._type)
        form.addRow("API endpoint", self._endpoint)
        form.addRow("API key env var", self._api_key_env)
        form.addRow("Local file", self._path)
        form.addRow("File patent column", self._patent_column)
        form.addRow("File CPC column", self._code_column)
        form.addRow("Network posture", self._offline)
        form.addRow("API batch size", self._batch_size)
        form.addRow("API rate limit (per min)", self._rate_limit)
        form.addRow("API max requests / run", self._max_calls)
        layout.addLayout(form)

        self._key_status = QLabel()
        self._key_status.setWordWrap(True)
        layout.addWidget(self._key_status)
        self._api_key_env.textChanged.connect(self._refresh_key_status)
        self._refresh_key_status()

        layout.addWidget(SectionLabel("Match defaults"))
        match_form = QFormLayout()
        self._grain = QComboBox()
        self._grain.addItems(_GRAINS)
        self._grain.setCurrentText(config.match.grain)
        self._metric = QComboBox()
        self._metric.addItems(_METRICS)
        self._metric.setCurrentText(config.match.overlap_metric)
        self._threshold = _float_spin(0.0, 1000.0, config.match.overlap_threshold)
        self._min_patents = _int_spin(1, 10_000, config.match.min_in_domain_patents)
        self._hit_floor = _float_spin(0.0, 1.0, config.match.hit_rate_floor, step=0.05)
        self._cache_path = QLineEdit(config.cache.path)
        self._cache_ttl = _int_spin(0, 3650, config.cache.ttl_days)
        match_form.addRow("Overlap grain", self._grain)
        match_form.addRow("Overlap metric", self._metric)
        match_form.addRow("Overlap threshold", self._threshold)
        match_form.addRow("Min in-domain patents", self._min_patents)
        match_form.addRow("Hit-rate floor (abort below)", self._hit_floor)
        match_form.addRow("Cache folder", self._cache_path)
        match_form.addRow("Cache TTL (days)", self._cache_ttl)
        layout.addLayout(match_form)

        location = QLabel(
            f"Saved to <code>{store.path()}</code>. Endpoint/auth verified as of "
            f"{CPC_ENDPOINT_AS_OF}; re-verify against data.uspto.gov."
        )
        location.setWordWrap(True)
        layout.addWidget(location)

        test = QPushButton("Check API key")
        test.clicked.connect(self._refresh_key_status)
        layout.addWidget(test)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _refresh_key_status(self) -> None:
        name = self._api_key_env.text().strip()
        present = bool(os.environ.get(name)) if name else False
        if not name:
            self._key_status.setText("⚠ No env-var name set for the API key.")
        elif present:
            self._key_status.setText(f"✓ ${name} is set in this environment.")
        else:
            self._key_status.setText(
                f"⚠ ${name} is not set — export it before enabling network fetch "
                f"(the key is never stored in the project)."
            )

    def config(self) -> CpcConfig:
        """Return the :class:`CpcConfig` reflecting the current form state."""
        config = CpcConfig()
        config.source.type = self._type.currentData()
        config.source.endpoint = self._endpoint.text().strip()
        config.source.api_key_env = self._api_key_env.text().strip()
        config.source.path = self._path.text().strip()
        config.source.patent_column = self._patent_column.text().strip() or "patent_id"
        config.source.code_column = self._code_column.text().strip() or "cpc_group"
        config.source.offline_only = bool(self._offline.currentData())
        config.source.batch_size = self._batch_size.value()
        config.source.rate_limit_per_min = self._rate_limit.value()
        config.source.max_api_calls = self._max_calls.value()
        config.match.grain = self._grain.currentText()  # type: ignore[assignment]  # from _GRAINS
        config.match.overlap_metric = self._metric.currentText()  # type: ignore[assignment]  # from _METRICS
        config.match.overlap_threshold = self._threshold.value()
        config.match.min_in_domain_patents = self._min_patents.value()
        config.match.hit_rate_floor = self._hit_floor.value()
        config.cache.path = self._cache_path.text().strip() or "data/cpc"
        config.cache.ttl_days = self._cache_ttl.value()
        return config

    def _save(self) -> None:
        self._store.save(self.config())
        self.accept()


def _int_spin(low: int, high: int, value: int) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(low, high)
    spin.setValue(value)
    return spin


def _float_spin(low: float, high: float, value: float, *, step: float = 1.0) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(low, high)
    spin.setSingleStep(step)
    spin.setDecimals(2)
    spin.setValue(value)
    return spin
