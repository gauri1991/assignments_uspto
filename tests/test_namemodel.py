"""Tests for the built-in pure-Python ML name classifier (uspto_assignments.namemodel).

These exercise the SHIPPED artifact + inference only, so they are deterministic and independent of
the gitignored PatentsView training data (which lives only on the dev machine).
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from uspto_assignments import classify_column, classify_name, classify_value, model_available
from uspto_assignments import namemodel as nm
from uspto_assignments.batch import _CLASSIFY_METHODS

# Pinned output of the featurizer for one input — guards against an accidental change to
# char_wb_ngrams that would silently desync training from inference.
_ACME_INC_NGRAMS = {
    " A",
    " AC",
    " ACM",
    " I",
    " IN",
    " INC",
    "AC",
    "ACM",
    "ACME",
    "C ",
    "CM",
    "CME",
    "CME ",
    "E ",
    "IN",
    "INC",
    "INC ",
    "ME",
    "ME ",
    "NC",
    "NC ",
}


def test_model_ships_and_is_available() -> None:
    assert isinstance(model_available(), bool)
    assert model_available() is True  # the artifact is vendored in the package


def test_model_method_is_a_valid_classify_method() -> None:
    assert "model" in _CLASSIFY_METHODS


def test_canonical_names_classify_correctly() -> None:
    cases = {
        "": "unknown",
        "SONY": "unknown",  # single bare token, no signal → gate keeps it unknown
        "QUALCOMM INCORPORATED": "company",  # legal-suffix gate
        "SAMSUNG ELECTRONICS CO., LTD.": "company",
        "SMITH, JOHN A": "individual",  # LAST, FIRST comma gate
        "JOHN SMITH": "individual",  # model-decided (no signal, no comma, multi-token)
        "THE BOARD OF TRUSTEES OF THE UNIVERSITY": "company",
    }
    for name, expected in cases.items():
        assert classify_name(name, method="model") == expected, name


def test_model_beats_rules_on_an_unsuffixed_company() -> None:
    """The whole point: a company with no legal suffix that the rules gate can't name."""
    name = "METAL WORKS RAMAT DAVID"
    assert (
        classify_name(name, method="rules") != "company"
    )  # rules can't (no signal, looks personal)
    assert classify_name(name, method="model") == "company"  # the model can


def test_dispatch_through_value_and_column() -> None:
    assert classify_value("SMITH, JOHN; ACME INC", method="model", separator="; ", mode="any") == (
        "company"
    )
    table = pa.table({"name": ["ACME INC", "SMITH, JOHN A", None, "SONY"]})
    out = classify_column(table, "name", "name_type", method="model")
    types = out.column("name_type").to_pylist()
    assert types == ["company", "individual", None, "unknown"]


def test_featurizer_output_is_pinned() -> None:
    assert nm.char_wb_ngrams("ACME INC") == _ACME_INC_NGRAMS


def test_falls_back_to_rules_when_artifact_unloadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing/broken artifact must degrade to rules, never raise."""
    nm._load_model.cache_clear()  # drop any real cached model; monkeypatch restores at teardown
    monkeypatch.setattr(nm, "_load_model", lambda: None)
    for name in ("JOHN SMITH", "ACME INC", "SMITH, JOHN A", "SONY", ""):
        assert nm.classify_name_model(name) == nm._classify_rules(name)


def test_artifact_schema_is_consistent() -> None:
    model = nm._load_model()
    assert model is not None
    assert len(model.ngram_weights) > 1000  # a real trained vocabulary shipped
    assert len(model.token_weights) == len(nm.TOKEN_FEATURES)
    assert 0.0 <= model.t_low <= model.t_high <= 1.0


def test_never_raises_on_odd_input() -> None:
    for name in ("", "   ", "123", "!!!", "A", "X" * 500, "SMITH,,,JOHN"):
        assert nm.classify_name_model(name) in {"company", "individual", "unknown"}
