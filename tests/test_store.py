"""Tests for the memory-mapped Arrow-IPC store (parse_to_store / open_store)."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from uspto_assignments import (
    STORE_TABLES,
    TableStore,
    columns_for,
    export_store,
    is_dataset_dir,
    open_dataset,
    open_parquet_store,
    open_store,
    parse_to_store,
    read_table_file,
)
from uspto_assignments_ui.settings import UiStateStore

FIXTURE = Path(__file__).parent / "fixtures" / "sample_assignment.xml"


def test_parse_to_store_row_counts(tmp_path: Path) -> None:
    store = parse_to_store(FIXTURE, tmp_path, batch_size=1)
    assert store.row_counts() == {
        "assignments": 2,
        "assignors": 3,
        "assignees": 2,
        "properties": 4,
        "flat": 4,
    }
    assert store.names == STORE_TABLES  # canonical order


def test_store_files_written_and_reopenable(tmp_path: Path) -> None:
    parse_to_store(FIXTURE, tmp_path)
    for name in STORE_TABLES:
        assert (tmp_path / f"{name}.arrow").is_file()

    reopened = open_store(tmp_path)
    assert reopened.row_counts()["properties"] == 4
    # leading zeros preserved through the Arrow round-trip
    assert reopened.table("assignments").column("reel_no").to_pylist() == ["012345", "054321"]


def test_is_dataset_dir_distinguishes_dataset_from_raw_input_folder(tmp_path: Path) -> None:
    arrow_ds = tmp_path / "arrow"
    parse_to_store(FIXTURE, arrow_ds)  # writes <table>.arrow files
    assert is_dataset_dir(arrow_ds) is True

    parquet_ds = tmp_path / "parquet"
    export_store(open_store(arrow_ds), parquet_ds, "parquet")  # writes <table>.parquet files
    assert is_dataset_dir(parquet_ds) is True

    raw = tmp_path / "raw"  # a folder of source files is NOT a dataset
    raw.mkdir()
    (raw / "a.xml").write_text("<x/>", encoding="utf-8")
    assert is_dataset_dir(raw) is False
    assert is_dataset_dir(tmp_path / "does_not_exist") is False


def test_flat_table_columns_and_rollups(tmp_path: Path) -> None:
    store = parse_to_store(FIXTURE, tmp_path)
    flat = store.table("flat")
    assert "assignor_names" in flat.column_names
    names = flat.column("assignor_names").to_pylist()
    assert "SMITH, JOHN; DOE, JANE" in names


def test_flat_carries_latest_execution_date(tmp_path: Path) -> None:
    store = parse_to_store(FIXTURE, tmp_path)
    flat = store.table("flat")
    assert "execution_date" in flat.column_names and "date_acknowledged" in flat.column_names
    # two assignors sign on 20231201 and 20231202 → the flat rollup is the latest signer date
    assert "20231202" in flat.column("execution_date").to_pylist()


def test_flat_transaction_date_prefers_execution_then_recorded(tmp_path: Path) -> None:
    store = parse_to_store(FIXTURE, tmp_path)
    rows = store.table("flat").to_pylist()
    # record 1 has signer dates → transaction_date is the latest execution date, sourced 'execution'
    signed = next(r for r in rows if r["reel_no"] == "012345")
    assert signed["transaction_date"] == "20231202" and signed["date_source"] == "execution"
    # record 2 (MERGER) has no signer date → falls back to recorded_date, sourced 'recorded'
    unsigned = next(r for r in rows if r["reel_no"] == "054321")
    assert unsigned["execution_date"] is None
    assert unsigned["transaction_date"] == "20230630" and unsigned["date_source"] == "recorded"


def test_progress_callback_reports_final_count(tmp_path: Path) -> None:
    seen: list[int] = []
    parse_to_store(FIXTURE, tmp_path, batch_size=1, progress=seen.append)
    assert seen[-1] == 2  # two assignments in the fixture


def test_open_store_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no Arrow-IPC store"):
        open_store(tmp_path)


def test_parse_to_store_respects_limit(tmp_path: Path) -> None:
    store = parse_to_store(FIXTURE, tmp_path, batch_size=1, limit=1)
    # only the first assignment is loaded (2 assignors, 1 assignee, 3 property rows)
    assert store.row_counts()["assignments"] == 1
    assert store.row_counts()["assignors"] == 2


def test_select_columns_projects_and_drops(tmp_path: Path) -> None:
    store = parse_to_store(FIXTURE, tmp_path)
    projected = store.select_columns({"properties": ["reel_no", "doc_number"], "flat": []})
    assert projected.table("properties").column_names == ["reel_no", "doc_number"]
    assert "flat" not in projected.names  # empty selection drops the table
    assert projected.table("assignors").num_columns == len(columns_for("assignors"))  # untouched


def test_columns_for_matches_store_schema(tmp_path: Path) -> None:
    store = parse_to_store(FIXTURE, tmp_path)
    for name in store.names:
        assert store.table(name).column_names == columns_for(name)


def test_parquet_store_roundtrip(tmp_path: Path) -> None:
    store = parse_to_store(FIXTURE, tmp_path / "arrow")
    export_store(store, tmp_path / "pq", "parquet")
    reopened = open_parquet_store(tmp_path / "pq")
    assert reopened.row_counts() == store.row_counts()
    # leading zeros survive the Parquet round-trip
    assert reopened.table("assignments").column("reel_no").to_pylist() == ["012345", "054321"]


def test_open_dataset_detects_arrow_then_parquet(tmp_path: Path) -> None:
    store = parse_to_store(FIXTURE, tmp_path / "arrow")
    assert open_dataset(tmp_path / "arrow").row_counts()["properties"] == 4  # arrow present

    export_store(store, tmp_path / "pq", "parquet")
    assert open_dataset(tmp_path / "pq").row_counts()["flat"] == 4  # parquet fallback


def test_open_dataset_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"no Arrow .* or Parquet"):
        open_dataset(tmp_path)


def test_ui_state_store_roundtrip_and_corrupt_file(tmp_path: Path) -> None:
    store = UiStateStore(tmp_path / "ui_state.json")
    assert store.last_dir("input") == ""  # unset
    store.set_last_dir("input", str(tmp_path))
    assert store.last_dir("input") == str(tmp_path)
    assert store.last_dir("output") == ""  # other keys unaffected

    store.set_last_dir("output", str(tmp_path / "gone"))  # not a directory -> reads as ""
    assert store.last_dir("output") == ""

    (tmp_path / "ui_state.json").write_text("{not json", encoding="utf-8")
    assert UiStateStore(tmp_path / "ui_state.json").last_dir("input") == ""  # corrupt -> ""


def _write_parquet(path: Path, table: pa.Table) -> None:
    pq.write_table(table, str(path))  # pyright: ignore[reportUnknownMemberType]


def test_read_table_file_stringifies_all_types(tmp_path: Path) -> None:
    table = pa.table(
        {
            "id": pa.array([1, 2], pa.int64()),
            "name": ["ACME", "GLOBEX"],
            "codes": pa.array([["H04L", "G06F"], ["A61F"]], pa.list_(pa.string())),
            "flag": [True, False],
        }
    )
    _write_parquet(tmp_path / "f.parquet", table)
    out = read_table_file(tmp_path / "f.parquet")
    assert all(isinstance(v, str) for v in out.column("id").to_pylist())  # everything is text now
    assert out.column("id").to_pylist() == ["1", "2"]
    assert out.column("codes").to_pylist() == ["H04L; G06F", "A61F"]  # list joined
    assert out.column("flag").to_pylist() == ["true", "false"]


def test_read_table_file_csv_and_unsupported(tmp_path: Path) -> None:
    (tmp_path / "g.csv").write_text("a,b\n1,hi\n2,yo\n", encoding="utf-8")
    assert read_table_file(tmp_path / "g.csv").to_pylist() == [
        {"a": "1", "b": "hi"},
        {"a": "2", "b": "yo"},
    ]
    (tmp_path / "x.txt").write_text("nope", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported file type"):
        read_table_file(tmp_path / "x.txt")  # extension not supported
    with pytest.raises(FileNotFoundError):
        read_table_file(tmp_path / "missing.parquet")


def test_table_store_names_include_non_canonical(tmp_path: Path) -> None:
    store = TableStore({"my_export": pa.table({"x": ["1"]})})
    assert store.names == ["my_export"]  # a viewer table (not one of the 5) is still listed
