"""Tests for output-path naming helpers (uspto_assignments.naming)."""

from __future__ import annotations

from pathlib import Path

from uspto_assignments import scope_suffix, unique_path


def test_scope_suffix() -> None:
    assert scope_suffix("all") == ""
    assert scope_suffix("filtered") == "_filtered"
    assert scope_suffix("selected") == "_selected"


def test_unique_path_returns_original_when_free(tmp_path: Path) -> None:
    target = tmp_path / "report.csv"
    assert unique_path(target) == target


def test_unique_path_increments_file_before_suffix(tmp_path: Path) -> None:
    (tmp_path / "report.csv").touch()
    assert unique_path(tmp_path / "report.csv") == tmp_path / "report (1).csv"
    (tmp_path / "report (1).csv").touch()
    assert unique_path(tmp_path / "report.csv") == tmp_path / "report (2).csv"


def test_unique_path_handles_directories(tmp_path: Path) -> None:
    (tmp_path / "ad20260709").mkdir()
    assert unique_path(tmp_path / "ad20260709") == tmp_path / "ad20260709 (1)"


def test_unique_path_preserves_multipart_suffix_stem(tmp_path: Path) -> None:
    (tmp_path / "ad.2026.parquet").touch()
    # only the final suffix is treated as the extension
    assert unique_path(tmp_path / "ad.2026.parquet") == tmp_path / "ad.2026 (1).parquet"
