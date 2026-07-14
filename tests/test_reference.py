"""Tests for the disambiguated-assignee reference gazetteer (uspto_assignments.reference)."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from uspto_assignments import (
    build_reference,
    extract_distinct_reference,
    load_reference,
    match_column,
)


def _write_tsv(path: Path) -> None:
    path.write_text(
        "disambig_assignee_organization\tassignee_id\n"
        "ADOBE SYSTEMS INCORPORATED\tA1\n"
        "MACROMEDIA INC\tA2\n"
        "\tA3\n"  # an individual assignee row: empty org -> excluded from the gazetteer
        "QUALCOMM INCORPORATED\tA4\n"
        "ADOBE SYSTEMS INCORPORATED\tA1\n",  # duplicate org -> deduped
        encoding="utf-8",
    )


def test_build_reference_dedupes_and_skips_individuals(tmp_path: Path) -> None:
    tsv = tmp_path / "ref.tsv"
    _write_tsv(tsv)
    gaz = build_reference(tsv, "disambig_assignee_organization", id_column="assignee_id")
    assert gaz.size() == 3  # 3 distinct non-empty organizations
    assert gaz.match("ADOBE SYSTEMS INCORPORATED") == ("ADOBE SYSTEMS INCORPORATED", "A1", 100)


def test_reference_match_exact_fuzzy_and_miss(tmp_path: Path) -> None:
    tsv = tmp_path / "ref.tsv"
    _write_tsv(tsv)
    gaz = build_reference(tsv, "disambig_assignee_organization", id_column="assignee_id")
    cleaned = gaz.match("Adobe Systems, Inc.")  # cleaned key fuzzy-matches the full org name
    assert cleaned[:2] == ("ADOBE SYSTEMS INCORPORATED", "A1")
    assert cleaned[2] >= 90
    assert gaz.match("QUALCOMM INC", threshold=80)[0] == "QUALCOMM INCORPORATED"  # fuzzy
    assert gaz.match("SMITH, JOHN") == (None, None, 0)  # unmatched -> presumed individual


def test_match_column_multi_party_any(tmp_path: Path) -> None:
    tsv = tmp_path / "ref.tsv"
    _write_tsv(tsv)
    gaz = build_reference(tsv, "disambig_assignee_organization", id_column="assignee_id")
    table = pa.table(
        {
            "assignor_names": [
                "ADOBE SYSTEMS INCORPORATED; SMITH, JOHN",
                "SMITH, JANE",
                "MACROMEDIA INC",
            ]
        }
    )
    result = match_column(
        table,
        "assignor_names",
        gaz,
        "assignor_names_disambiguated",
        "assignor_names_matched",
        "assignor_names_assignee_id",
        separator="; ",
        mode="any",
    )
    assert result.column("assignor_names_matched").to_pylist() == ["true", "false", "true"]
    # matched parts are replaced by the disambiguated name; unmatched parts kept as-is
    assert (
        result.column("assignor_names_disambiguated").to_pylist()[0]
        == "ADOBE SYSTEMS INCORPORATED; SMITH, JOHN"
    )
    assert result.column("assignor_names_assignee_id").to_pylist() == ["A1", "", "A2"]


def test_match_column_mode_all(tmp_path: Path) -> None:
    tsv = tmp_path / "ref.tsv"
    _write_tsv(tsv)
    gaz = build_reference(tsv, "disambig_assignee_organization")
    table = pa.table({"names": ["ADOBE SYSTEMS INCORPORATED; SMITH, JOHN"]})
    result = match_column(
        table, "names", gaz, "names_disambiguated", "names_matched", "", separator="; ", mode="all"
    )
    assert result.column("names_matched").to_pylist() == ["false"]  # not every party is a company


def test_extract_distinct_reference_and_reload(tmp_path: Path) -> None:
    tsv = tmp_path / "ref.tsv"
    _write_tsv(tsv)
    compact = tmp_path / "compact.parquet"
    count = extract_distinct_reference(
        tsv, compact, name_column="disambig_assignee_organization", id_column="assignee_id"
    )
    assert count == 3
    written = pq.read_table(compact)  # pyright: ignore[reportUnknownMemberType]
    assert set(written.column_names) == {"organization", "assignee_id"}
    # the compact file reloads through the standard loader with default column names
    gaz = load_reference(compact, "organization", id_column="assignee_id")
    assert gaz.match("MACROMEDIA INC") == ("MACROMEDIA INC", "A2", 100)


def test_load_reference_caches_until_mtime_changes(tmp_path: Path) -> None:
    tsv = tmp_path / "ref.tsv"
    _write_tsv(tsv)
    first = load_reference(tsv, "disambig_assignee_organization")
    again = load_reference(tsv, "disambig_assignee_organization")
    assert first is again  # cached instance reused (same path + mtime + columns)


def test_match_column_emits_score_and_review_columns(tmp_path: Path) -> None:
    tsv = tmp_path / "ref.tsv"
    _write_tsv(tsv)
    gaz = build_reference(tsv, "disambig_assignee_organization", id_column="assignee_id")
    table = pa.table({"name": ["ADOBE SYSTEMS INCORPORATED", "QUALCOMM INC", "SMITH, JOHN", None]})
    result = match_column(
        table,
        "name",
        gaz,
        "name_disambiguated",
        "name_matched",
        "name_assignee_id",
        threshold=80,
        score_col="name_match_score",
        review_col="name_match_review",
        review_threshold=99,
    )
    scores = result.column("name_match_score").to_pylist()
    review = result.column("name_match_review").to_pylist()
    assert scores[0] == 100 and review[0] == "false"  # exact
    assert scores[1] is not None and 80 <= scores[1] < 99  # fuzzy -> review band
    assert review[1] == "true"
    assert scores[2] == 0 and review[2] == "false"  # unmatched: not a review case
    assert scores[3] is None and review[3] is None
