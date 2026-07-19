"""Settings dialog for the CPC data source, cache, and match defaults.

Edits a :class:`~uspto_assignments.CpcConfig` and persists it to the project config file (via
:class:`~uspto_assignments_ui.settings.CpcConfigStore`). The API key is never entered or stored
here — only the name of the environment variable that holds it.
"""

from __future__ import annotations

import os

from PyQt6.QtCore import QThread
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from uspto_assignments import CpcConfig, make_source
from uspto_assignments.cpcconfig import CPC_ENDPOINT_AS_OF

from ..settings import CpcConfigStore
from ..workers import CallWorker
from .page import SectionLabel

# A known granted patent used to probe the live API (has CPC codes).
_TEST_PATENT = "10000000"

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
        self._thread: QThread | None = None  # live Test-connection probe (off the GUI thread)
        self._worker: CallWorker | None = None
        self.setWindowTitle("CPC data source — USPTO Open Data Portal API")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(
            SectionLabel("CPC data source (USPTO Open Data Portal / PatentSearch API)")
        )

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
        test = QPushButton("Check API key")
        test.clicked.connect(self._refresh_key_status)
        self._test_btn = QPushButton("Test connection")
        self._test_btn.setToolTip(
            f"Fetch CPC codes for patent {_TEST_PATENT} from the live API to confirm the key + "
            "endpoint work (uses one API call)."
        )
        self._test_btn.clicked.connect(self._test_connection)
        key_row = QHBoxLayout()
        key_row.addWidget(self._key_status, 1)
        key_row.addWidget(test)
        key_row.addWidget(self._test_btn)
        layout.addLayout(key_row)
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
        # Buyer-ranking weights (overlap strength × recency × in-domain volume) — previously
        # documented as configured here but only editable by hand-editing cpc_config.json.
        self._weight_overlap = _float_spin(0.0, 100.0, config.match.weight_overlap, step=0.1)
        self._weight_recency = _float_spin(0.0, 100.0, config.match.weight_recency, step=0.1)
        self._weight_volume = _float_spin(0.0, 100.0, config.match.weight_volume, step=0.1)
        match_form.addRow("Overlap grain", self._grain)
        match_form.addRow("Overlap metric", self._metric)
        match_form.addRow("Overlap threshold", self._threshold)
        match_form.addRow("Min in-domain patents", self._min_patents)
        match_form.addRow("Hit-rate floor (abort below)", self._hit_floor)
        match_form.addRow("Ranking weight — overlap", self._weight_overlap)
        match_form.addRow("Ranking weight — recency", self._weight_recency)
        match_form.addRow("Ranking weight — volume", self._weight_volume)
        layout.addLayout(match_form)

        layout.addWidget(SectionLabel("Cache"))
        cache_form = QFormLayout()
        self._cache_path = QLineEdit(config.cache.path)
        self._cache_ttl = _int_spin(0, 3650, config.cache.ttl_days)
        cache_form.addRow("Cache folder", self._cache_path)
        cache_form.addRow("Cache TTL (days)", self._cache_ttl)
        layout.addLayout(cache_form)

        location = QLabel(
            f"Live endpoint: <code>api.uspto.gov</code> (X-API-KEY header). Saved to "
            f"<code>{store.path()}</code>. Endpoint/auth verified as of {CPC_ENDPOINT_AS_OF}; "
            f"re-verify against data.uspto.gov."
        )
        location.setWordWrap(True)
        layout.addWidget(location)

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

    def _test_connection(self) -> None:
        """Live-probe the API: fetch one known patent's CPC codes off the GUI thread."""
        if self._thread is not None:
            return
        source_config = self.config().source
        if source_config.type != "local_file":  # force the network on for the probe only
            source_config.offline_only = False
        self._test_btn.setEnabled(False)
        self._key_status.setText(f"Testing… fetching CPC for patent {_TEST_PATENT}")
        config = CpcConfig()
        config.source = source_config
        thread = QThread(self)
        worker = CallWorker(lambda: make_source(config).fetch([_TEST_PATENT]))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_test_ready)
        worker.failed.connect(self._on_test_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._cleanup_test)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _on_test_ready(self, result: object) -> None:
        codes = result.get(_TEST_PATENT, []) if isinstance(result, dict) else []
        if codes:
            preview = ", ".join(codes[:3]) + ("…" if len(codes) > 3 else "")
            self._key_status.setText(
                f"✓ Connected — patent {_TEST_PATENT}: {len(codes)} CPC ({preview})"
            )
        else:
            self._key_status.setText(
                f"⚠ Connected but patent {_TEST_PATENT} returned no CPC codes — check the endpoint "
                "and the response field."
            )

    def _on_test_failed(self, message: str) -> None:
        self._key_status.setText(f"✗ Test failed: {message}")

    def _cleanup_test(self) -> None:
        if self._thread is not None:
            self._thread.deleteLater()
        self._thread = None
        self._worker = None
        self._test_btn.setEnabled(True)

    def _busy(self) -> bool:
        """True (with a notice) while the test probe runs — closing would kill its live thread."""
        if self._thread is None:
            return False
        QMessageBox.information(self, "Busy", "Wait for the connection test to finish.")
        return True

    def closeEvent(self, a0: QCloseEvent | None) -> None:
        """Never destroy the dialog while the test-connection thread is alive."""
        if self._busy():
            if a0 is not None:
                a0.ignore()
            return
        super().closeEvent(a0)

    def reject(self) -> None:
        """Route Esc/Cancel through the busy guard."""
        if not self._busy():
            super().reject()

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
        config.match.weight_overlap = self._weight_overlap.value()
        config.match.weight_recency = self._weight_recency.value()
        config.match.weight_volume = self._weight_volume.value()
        config.cache.path = self._cache_path.text().strip() or "data/cpc"
        config.cache.ttl_days = self._cache_ttl.value()
        return config

    def _save(self) -> None:
        if self._busy():
            return
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
