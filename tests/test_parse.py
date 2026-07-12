"""Behavioral tests for USPTO assignment XML parsing and output writing."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from lxml import etree

from uspto_assignments import (
    ExtractedRecord,
    extract,
    iter_assignments,
    iter_records,
    write_excel,
    write_parquet,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_assignment.xml"


@pytest.fixture
def records() -> list[ExtractedRecord]:
    return list(iter_records(FIXTURE))


def test_streams_all_assignments() -> None:
    assert sum(1 for _ in iter_assignments(FIXTURE)) == 2


def test_assignment_header_fields(records: list[ExtractedRecord]) -> None:
    first = records[0].assignment
    assert first.reel_no == "012345"
    assert first.frame_no == "0678"
    assert first.recorded_date == "20240110"  # nested <date> extracted
    assert first.last_update_date == "20240115"
    assert first.page_count == "4"
    assert first.conveyance_text == "ASSIGNMENT OF ASSIGNORS INTEREST"
    assert first.correspondent_name == "ACME IP LAW GROUP"
    assert first.correspondent_address_2 == "SUITE 200"


def test_multiple_assignors_and_single_assignee(records: list[ExtractedRecord]) -> None:
    first = records[0]
    assert [a.name for a in first.assignors] == ["SMITH, JOHN", "DOE, JANE"]
    assert first.assignors[0].execution_date == "20231201"
    assert first.assignors[0].date_acknowledged == "20231205"
    assert first.assignors[1].date_acknowledged is None
    assert len(first.assignees) == 1
    assert first.assignees[0].city == "SAN JOSE"
    assert first.assignees[0].country_name == "USA"


def test_multiple_document_ids_expand_to_rows(records: list[ExtractedRecord]) -> None:
    # First property has 2 document-ids, second has 1 -> 3 property rows total.
    props = records[0].properties
    assert len(props) == 3
    assert props[0].doc_number == "16123456"
    assert props[0].doc_kind == "X0"
    assert props[0].doc_name == "WIDGET CORP"  # optional document-id/name captured
    assert props[0].doc_date == "20200401"
    assert props[1].doc_number == "10987654"
    assert props[2].invention_title == "IMPROVED WIDGET COATING"


def test_invention_title_flattens_inline_markup(records: list[ExtractedRecord]) -> None:
    # Title is "WIDGET <b>FASTENING</b> MECHANISM" — must not be truncated at the first tag.
    assert records[0].properties[0].invention_title == "WIDGET FASTENING MECHANISM"


def test_missing_optional_fields_are_none(records: list[ExtractedRecord]) -> None:
    second = records[1]
    assert second.assignment.last_update_date is None  # absent in fixture
    assert second.assignment.correspondent_name is None  # no correspondent element
    assert second.assignors[0].execution_date is None  # assignor without execution-date
    assert second.assignees[0].address_1 is None


def test_property_without_document_id_still_emits_row(records: list[ExtractedRecord]) -> None:
    second = records[1]
    assert len(second.properties) == 1
    row = second.properties[0]
    assert row.invention_title == "TITLE ONLY NO DOCID"
    assert row.doc_number is None
    assert row.reel_no == "054321"  # key still propagated


def test_child_rows_carry_parent_key(records: list[ExtractedRecord]) -> None:
    for rec in records:
        key = (rec.assignment.reel_no, rec.assignment.frame_no)
        for child in [*rec.assignors, *rec.assignees, *rec.properties]:
            assert (child.reel_no, child.frame_no) == key


def test_reads_xml_directly_from_zip(tmp_path: Path) -> None:
    # USPTO ships these as .zip; we should parse the contained .xml without extracting it.
    zip_path = tmp_path / "ad_sample.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(FIXTURE, arcname="ad_sample.xml")
    records = list(iter_records(zip_path))
    assert [r.assignment.reel_no for r in records] == ["012345", "054321"]


def test_zip_without_xml_member_raises(tmp_path: Path) -> None:
    zip_path = tmp_path / "empty.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("readme.txt", "no xml here")
    with pytest.raises(ValueError, match=r"no \.xml member"):
        list(iter_records(zip_path))


def test_recovers_from_minor_malformation(tmp_path: Path) -> None:
    # An unescaped ampersand would break a strict parser; recover=True should skip past it
    # and still yield the well-formed records.
    broken = tmp_path / "broken.xml"
    broken.write_text(
        "<patent-assignments>"
        "<patent-assignment><assignment-record><reel-no>111</reel-no>"
        "<frame-no>222</frame-no></assignment-record></patent-assignment>"
        "<patent-assignment><assignment-record><reel-no>333</reel-no>"
        "<frame-no>444</frame-no></assignment-record></patent-assignment>"
        "</patent-assignments>",
        encoding="utf-8",
    )
    reels = [r.assignment.reel_no for r in iter_records(broken)]
    assert reels == ["111", "333"]


def test_extract_falls_back_to_assignment_element_for_key() -> None:
    # Some dumps put reel/frame directly under patent-assignment (no assignment-record wrapper).
    xml = (
        "<patent-assignment><reel-no>999</reel-no><frame-no>000</frame-no>"
        "<patent-assignors><patent-assignor><name>X CO</name></patent-assignor>"
        "</patent-assignors></patent-assignment>"
    )
    rec = extract(etree.fromstring(xml))
    assert rec.assignment.reel_no == "999"
    assert rec.assignors[0].reel_no == "999"


def test_write_parquet_roundtrip(tmp_path: Path) -> None:
    counts = write_parquet(iter_records(FIXTURE), tmp_path, "assignments", batch_size=1)
    assert counts == {"assignments": 2, "assignors": 3, "assignees": 2, "properties": 4}

    assignments = pq.read_table(tmp_path / "assignments.parquet").to_pydict()  # pyright: ignore[reportUnknownMemberType]
    assert assignments["reel_no"] == ["012345", "054321"]

    properties = pq.read_table(tmp_path / "properties.parquet")  # pyright: ignore[reportUnknownMemberType]
    assert properties.num_rows == 4
    assert "doc_number" in properties.column_names


def test_write_excel_creates_all_sheets(tmp_path: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    counts = write_excel(iter_records(FIXTURE), tmp_path, "assignments")
    out = tmp_path / "assignments.xlsx"
    assert out.is_file()
    assert set(counts) == {"assignments", "assignors", "assignees", "properties", "flat"}
    assert counts["properties"] == 4

    wb = openpyxl.load_workbook(out)
    assert set(wb.sheetnames) == {"assignments", "assignors", "assignees", "properties", "flat"}
    # flat view has one row per property (4) and concatenates assignor names.
    flat = wb["flat"]
    header = [c.value for c in next(flat.iter_rows(max_row=1))]
    assert "assignor_names" in header
