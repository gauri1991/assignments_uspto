"""Tests for the assignment_uspto script logic."""

from __future__ import annotations

import pytest

from main import summarize


def test_summarize_basic() -> None:
    result = summarize([1, 2, 3, 4, 5])
    assert result == {"count": 5, "total": 15, "mean": 3.0, "min": 1, "max": 5}


def test_summarize_single_value() -> None:
    result = summarize([42])
    assert result["count"] == 1
    assert result["mean"] == 42.0


def test_summarize_empty_raises() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        summarize([])
