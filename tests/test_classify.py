"""Tests for entity-type classification (uspto_assignments.classify)."""

from __future__ import annotations

import pyarrow as pa

from uspto_assignments import classify_column, classify_name, classify_value


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
    # probablepeople is an optional dependency; without it, classification must not crash.
    assert classify_name("MACROMEDIA, INC.", method="probablepeople") in {
        "company",
        "individual",
        "unknown",
    }
