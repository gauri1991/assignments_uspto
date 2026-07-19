"""Tests for entity-type classification (uspto_assignments.classify)."""

from __future__ import annotations

import importlib.util
import sys
import types

import pyarrow as pa
import pytest

from uspto_assignments import (
    BatchEvent,
    ClassifyStep,
    EntityMemory,
    classify,
    classify_column,
    classify_name,
    classify_value,
    probablepeople_available,
    tag_memory,
)
from uspto_assignments import batch as batch_mod
from uspto_assignments.batch import _apply_classify


def test_company_names_by_legal_suffix() -> None:
    for name in [
        "MACROMEDIA, INC.",
        "ADOBE SYSTEMS INCORPORATED",
        "SAMSUNG ELECTRONICS CO., LTD.",
        "QUALCOMM INCORPORATED",
        "KABUSHIKI KAISHA TOSHIBA",
        "THE BOARD OF TRUSTEES OF THE UNIVERSITY",
    ]:
        assert classify_name(name) == "company", name


def test_individual_names_by_person_pattern() -> None:
    for name in ["SMITH, JOHN A.", "DE LA CRUZ, MARIA", "JOHN SMITH", "OConnor, Sean"]:
        assert classify_name(name) == "individual", name


def test_ambiguous_single_token_is_unknown() -> None:
    assert classify_name("SONY") == "unknown"  # single-token brand — deliberately not guessed
    assert classify_name("") == "unknown"


def test_classify_value_multi_party_modes() -> None:
    both_company = "FOO CORP; BAR LLC"
    mixed = "SMITH, JOHN; ACME INC"
    assert classify_value(both_company, separator="; ", mode="all") == "company"
    assert classify_value(mixed, separator="; ", mode="all") == "unknown"  # parties disagree
    assert classify_value(mixed, separator="; ", mode="any") == "company"
    assert classify_value(mixed, separator="; ", mode="first") == "individual"


def test_classify_column_adds_type_and_maps_distinct() -> None:
    table = pa.table(
        {"assignor_names": ["MACROMEDIA, INC.", "SMITH, JOHN", "MACROMEDIA, INC.", None]}
    )
    progress: list[tuple[int, int]] = []
    result = classify_column(
        table,
        "assignor_names",
        "assignor_names_type",
        separator="; ",
        on_progress=lambda d, t: progress.append((d, t)),
    )
    types = result.column("assignor_names_type").to_pylist()
    assert types == ["company", "individual", "company", None]
    assert progress and progress[-1][0] == progress[-1][1]  # final (total, total) reported


def test_probablepeople_method_falls_back_to_rules_when_absent() -> None:
    # Whether or not probablepeople is installed, the ML method must never crash.
    assert classify_name("MACROMEDIA, INC.", method="probablepeople") in {
        "company",
        "individual",
        "unknown",
    }


def test_tag_memory_tags_all_canonicals_by_rules() -> None:
    memory = EntityMemory(canonicals=["ACME INC", "SMITH, JOHN", "SONY"])
    tagged = tag_memory(memory, method="rules")
    assert tagged == 3
    assert memory.entity_type("ACME INC") == "company"
    assert memory.entity_type("SMITH, JOHN") == "individual"
    assert memory.entity_type("SONY") == "unknown"


def test_tag_memory_only_missing_preserves_existing_tags() -> None:
    memory = EntityMemory(canonicals=["ACME INC", "SMITH, JOHN"])
    memory.set_type("ACME INC", "individual")  # a deliberate manual override
    tagged = tag_memory(memory, method="rules", only_missing=True)
    assert tagged == 1  # only the untagged "SMITH, JOHN" was classified
    assert memory.entity_type("ACME INC") == "individual"  # manual tag left intact
    assert memory.entity_type("SMITH, JOHN") == "individual"


def test_probablepeople_available_returns_bool() -> None:
    assert isinstance(probablepeople_available(), bool)


def test_probablepeople_tags_real_names_when_installed() -> None:
    pytest.importorskip("probablepeople")
    # With the CRF model present, unambiguous corporation/person names classify correctly.
    assert classify_name("QUALCOMM INCORPORATED", method="probablepeople") == "company"
    assert classify_name("SMITH, JOHN A", method="probablepeople") == "individual"


def test_doublemetaphone_shim_backs_probablepeople_with_metaphone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On Pythons without a doublemetaphone C wheel (e.g. 3.14), the shim registers a stand-in
    # module backed by the pure-Python `metaphone` package so probablepeople can import.
    # Make any real/stale doublemetaphone absent so the shim runs its registration path.
    # (monkeypatch.delitem records the old value and restores it at teardown.)
    monkeypatch.delitem(sys.modules, "doublemetaphone", raising=False)
    try:
        real_installed = importlib.util.find_spec("doublemetaphone") is not None
    except ValueError:  # a spec-less module lingered under the name
        real_installed = False
    if real_installed:
        pytest.skip("real doublemetaphone installed — the shim is a no-op here")

    fake_metaphone = types.ModuleType("metaphone")
    fake_metaphone.doublemetaphone = lambda token: ("TEST", "")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "metaphone", fake_metaphone)

    classify._install_doublemetaphone_shim()

    registered = sys.modules.get("doublemetaphone")
    assert registered is not None
    assert registered.doublemetaphone("x") == ("TEST", "")  # type: ignore[attr-defined]


def test_apply_classify_warns_once_when_ml_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate probablepeople being absent: _apply_classify must emit exactly one visible warning
    # and still produce output (via the rules fallback), not fail or spam per name.
    monkeypatch.setattr(batch_mod, "probablepeople_available", lambda: False)
    table = pa.table({"assignor_names": ["MACROMEDIA, INC.", "SMITH, JOHN", "ACME LLC"]})
    tables = {"flat": table}
    events: list[BatchEvent] = []
    step = ClassifyStep(table="flat", column="assignor_names", method="probablepeople")
    _apply_classify(tables, step, events.append)
    warnings = [
        e for e in events if e.level == "warning" and "probablepeople not installed" in e.message
    ]
    assert len(warnings) == 1
    assert "assignor_names_type" in tables["flat"].column_names  # rules fallback still ran


def test_load_probablepeople_returns_none_on_non_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The exact Windows failure mode: probablepeople is present but its python-crfsuite C extension
    # fails to load with a NON-ImportError (e.g. "OSError: DLL load failed"). The broadened except
    # must catch it so classify falls back to rules instead of crashing the run.
    class _BoomFinder:
        def find_spec(self, name: str, path: object = None, target: object = None) -> None:
            if name == "probablepeople":
                raise OSError("simulated DLL load failure")
            # returning None (implicitly) tells the import system to try the next finder

    classify._load_probablepeople.cache_clear()  # @cache — force a fresh load attempt
    monkeypatch.delitem(sys.modules, "probablepeople", raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BoomFinder(), *sys.meta_path])
    try:
        assert classify._load_probablepeople() is None  # broad except → None, not a raise
        # classify_name must not raise and must return exactly the rules result for the same name
        got = classify_name("SMITH, JOHN A", method="probablepeople")
        assert got == classify_name("SMITH, JOHN A", method="rules")
    finally:
        classify._load_probablepeople.cache_clear()  # don't poison other tests' cached load
