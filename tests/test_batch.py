"""Tests for the batch engine (uspto_assignments.batch)."""

from __future__ import annotations

import json
import shutil
import zipfile
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from uspto_assignments import (
    AggregateStep,
    BatchEvent,
    BatchStep,
    BatchTemplate,
    ClassifyStep,
    CompareStep,
    DedupeStep,
    DeriveStep,
    EntityMemory,
    ExportStep,
    FilterClause,
    FilterStep,
    LoadConfig,
    NormalizeStep,
    ReferenceMatchStep,
    SelectStep,
    SortStep,
    TransferTypeStep,
    columns_after,
    describe_step,
    dump_templates,
    load_templates,
    parse_to_store,
    run_batch,
    run_preview,
    validate_template,
)
from uspto_assignments.batch import (
    FileResult,
    _apply_aggregate,
    _apply_classify,
    _apply_compare,
    _apply_dedupe,
    _apply_derive,
    _apply_normalize,
    _apply_reference_match,
    _apply_select,
    _apply_sort,
    _apply_step,
    _apply_transfer_type,
    _assign_source_dirs,
    _future_result,
    _needed_tables,
)
from uspto_assignments.filters import filter_sort


class _Sink:
    """Collects emitted event messages for assertions on step behavior."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, event: BatchEvent) -> None:
        self.messages.append(event.message)

    @property
    def text(self) -> str:
        return "\n".join(self.messages)


FIXTURE = Path(__file__).parent / "fixtures" / "sample_assignment.xml"


# -- v6: schema propagation, validation, export columns, enabled, preview ----


def test_columns_after_propagates_derived_columns() -> None:
    steps = [
        NormalizeStep(table="flat", column="assignor_names"),
        ClassifyStep(table="flat", column="assignor_names"),
        ReferenceMatchStep(table="flat", column="assignor_names", id_column="assignee_id"),
    ]
    cols = columns_after(LoadConfig(), steps, upto=3)["flat"]
    for expected in (
        "assignor_names_canonical",
        "assignor_names_type",
        "assignor_names_disambiguated",
        "assignor_names_matched",
        "assignor_names_assignee_id",
    ):
        assert expected in cols, expected
    # only steps before `upto` count
    assert "assignor_names_type" not in columns_after(LoadConfig(), steps, upto=1)["flat"]


def test_columns_after_aggregate_creates_summary_table() -> None:
    steps = [AggregateStep(table="assignees", group_by=["name"], count_distinct="name")]
    cols = columns_after(LoadConfig(), steps, upto=1)
    assert cols["assignees_by_name"] == ["name", "count", "name_distinct"]


def test_validate_template_flags_missing_column_and_reference() -> None:
    steps = [
        FilterStep(
            table="flat", clauses=[FilterClause("assignor_names_canonical", "contains", "X")]
        ),
        ReferenceMatchStep(table="flat", column="assignor_names", reference_path="/no/such.tsv"),
    ]
    warnings = validate_template(LoadConfig(), steps)
    assert any("assignor_names_canonical" in w for w in warnings)
    assert any("reference file not found" in w for w in warnings)


def test_export_columns_order_and_rename_and_enabled(tmp_path: Path) -> None:
    template = BatchTemplate(
        name="x",
        steps=[
            NormalizeStep(table="assignees", column="name"),
            ExportStep(
                fmt="csv",
                tables=["assignees"],
                columns={"assignees": ["name", "name_canonical"]},
                renames={"assignees": {"name_canonical": "clean_name"}},
            ),
        ],
    )
    run_batch(template, [FIXTURE], tmp_path / "out", timestamp="v6")
    header = (
        (tmp_path / "out" / "x" / "sample_assignment" / "assignees.csv")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert header == '"name","clean_name"'  # only chosen columns, in order, renamed


def test_disabled_step_is_skipped_on_run(tmp_path: Path) -> None:
    normalize = NormalizeStep(table="assignees", column="name")
    normalize.enabled = False
    template = BatchTemplate(
        name="d", steps=[normalize, ExportStep(fmt="csv", tables=["assignees"])]
    )
    run_batch(template, [FIXTURE], tmp_path / "out", timestamp="v6d")
    header = (
        (tmp_path / "out" / "d" / "sample_assignment" / "assignees.csv")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert "name_canonical" not in header  # the disabled normalize did not run


def test_silent_empty_guard_warns_on_run(tmp_path: Path) -> None:
    template = BatchTemplate(
        name="empty",
        steps=[
            FilterStep(table="assignees", clauses=[FilterClause("name", "equals", "___none___")]),
            ExportStep(fmt="csv", tables=["assignees"]),
        ],
    )
    events: list[BatchEvent] = []
    run_batch(template, [FIXTURE], tmp_path / "out", timestamp="e", on_event=events.append)
    warnings = [e for e in events if e.level == "error" and "EMPTY" in e.message]
    assert warnings, "a step that zeroed a table should emit a red warning"


def test_silent_empty_guard_flagged_in_preview() -> None:
    template = BatchTemplate(
        name="empty",
        steps=[
            FilterStep(table="assignees", clauses=[FilterClause("name", "equals", "___none___")]),
            ExportStep(fmt="csv", tables=["assignees"]),
        ],
    )
    _tables, stats = run_preview(template, FIXTURE, limit=50)
    filter_stat = stats[0]
    assert filter_stat.rows_after == 0 < filter_stat.rows_before
    assert "dropped all rows" in filter_stat.note


def test_bundled_template_files_load_and_roundtrip() -> None:
    templates_dir = Path(__file__).resolve().parent.parent / "templates"
    files = sorted(templates_dir.glob("*.json"))
    assert files, "expected bundled template files under templates/"
    for path in files:
        templates = load_templates(path)
        assert templates, f"{path.name} produced no templates"
        for template in templates:  # every step decodes and re-serializes cleanly
            assert template.to_dict()["steps"]


def test_firm_to_firm_templates_key_time_axis_on_transaction_date() -> None:
    """Regression: the firm-to-firm / CPC templates must derive their year from
    ``transaction_date`` (the true-event axis), not ``recorded_date`` (which lags the deal),
    and export ``execution_date`` alongside it. Guards against a silent revert."""
    templates_dir = Path(__file__).resolve().parent.parent / "templates"
    files = [
        "01_firm_to_firm_transactions_enriched.json",
        "06_firm_to_firm_rules_only.json",
        "07_cpc_patent_list_per_buyer.json",
        "buyer_identification_templates.json",
    ]
    checked = 0
    for name in files:
        for template in load_templates(templates_dir / name):
            year_derives = [
                s for s in template.steps if isinstance(s, DeriveStep) and s.op == "year"
            ]
            if not year_derives:  # leaderboard templates have no time axis — skip
                continue
            for step in year_derives:
                assert step.source == "transaction_date", (
                    f"{name}/{template.name}: year derived from {step.source!r}"
                )
            flat_cols = [
                col
                for export in template.steps
                if isinstance(export, ExportStep) and export.columns
                for col in (export.columns.get("flat") or [])
            ]
            assert "execution_date" in flat_cols, (
                f"{name}/{template.name}: export omits execution_date"
            )
            checked += 1
    assert checked, "expected at least one firm-to-firm template with a year-derive step"


def test_run_preview_returns_tables_and_stats() -> None:
    template = BatchTemplate(
        name="p",
        steps=[
            NormalizeStep(table="assignees", column="name"),
            ClassifyStep(table="assignees", column="name"),
            ExportStep(fmt="parquet", tables=["assignees"]),
        ],
    )
    tables, stats = run_preview(template, FIXTURE, limit=50)
    assert "name_canonical" in tables["assignees"].column_names
    assert "name_type" in tables["assignees"].column_names
    labels = [s.label for s in stats]
    assert any(s.columns_added == ["name_canonical"] for s in stats)
    assert stats[-1].note.startswith("export")  # export is skipped in preview
    assert labels  # a stat per step


def _granted_template() -> BatchTemplate:
    return BatchTemplate(
        name="granted",
        load=LoadConfig(),
        steps=[
            FilterStep(table="properties", clauses=[FilterClause("doc_kind", "equals", "B2")]),
            ExportStep(fmt="parquet", tables=["properties"]),
        ],
    )


def test_run_batch_sequential_writes_folder_per_source(tmp_path: Path) -> None:
    events: list[BatchEvent] = []
    result = run_batch(
        _granted_template(), [FIXTURE], tmp_path / "out", timestamp="t1", on_event=events.append
    )
    assert (result.succeeded, result.failed) == (1, 0)

    out = tmp_path / "out" / "granted" / "sample_assignment" / "properties.parquet"
    assert out.is_file()
    reopened = pq.read_table(out)  # pyright: ignore[reportUnknownMemberType]  # stub embeds Unknown
    assert reopened.num_rows == 1  # only the B2 (granted) row survives the filter
    assert (tmp_path / "out" / "granted" / "run_t1.log").is_file()

    messages = [e.message for e in events]
    assert any("filter properties" in m for m in messages)
    assert any("export properties" in m for m in messages)
    assert any("parsing" in m for m in messages)  # per-loop parse progress is surfaced
    assert any("done (" in m and m.endswith("s)") for m in messages)  # per-file elapsed
    assert any(m.startswith("Batch complete") and m.endswith("s") for m in messages)  # total
    assert result.results[0].elapsed >= 0.0


def test_run_batch_isolates_per_file_errors(tmp_path: Path) -> None:
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("readme.txt", "no xml")
    result = run_batch(_granted_template(), [FIXTURE, bad], tmp_path / "out", timestamp="t2")
    assert (result.succeeded, result.failed) == (1, 1)
    failed = next(r for r in result.results if not r.ok)
    assert failed.error and "no .xml member" in failed.error


def test_run_batch_accepts_dataset_folder_input(tmp_path: Path) -> None:
    parse_to_store(FIXTURE, tmp_path / "store")  # an Arrow dataset folder
    template = BatchTemplate(name="copy", steps=[ExportStep(fmt="csv", tables=["assignors"])])
    result = run_batch(template, [tmp_path / "store"], tmp_path / "out", timestamp="t3")
    assert result.succeeded == 1
    assert (tmp_path / "out" / "copy" / "store" / "assignors.csv").is_file()


def test_run_batch_parallel_workers(tmp_path: Path) -> None:
    a = tmp_path / "a.xml"
    b = tmp_path / "b.xml"
    shutil.copy(FIXTURE, a)
    shutil.copy(FIXTURE, b)
    events: list[BatchEvent] = []
    result = run_batch(
        _granted_template(),
        [a, b],
        tmp_path / "out",
        workers=2,
        timestamp="t4",
        on_event=events.append,
    )
    assert result.succeeded == 2
    assert (tmp_path / "out" / "granted" / "a" / "properties.parquet").is_file()
    assert (tmp_path / "out" / "granted" / "b" / "properties.parquet").is_file()

    messages = [e.message for e in events]
    # each worker's events are labelled with their file, and a combined total is shown
    assert any("[a.xml]" in m for m in messages)
    assert any("[b.xml]" in m for m in messages)
    assert any("across" in m and "records" in m for m in messages)
    assert any("pid" in m for m in messages)  # worker PIDs prove real parallel processes


def test_assign_source_dirs_disambiguates_same_stem(tmp_path: Path) -> None:
    inputs = [tmp_path / "a" / "x.xml", tmp_path / "b" / "x.xml", tmp_path / "y.xml"]
    (tmp_path / "out" / "y").mkdir(parents=True)  # an existing dir must also be skipped
    dirs = _assign_source_dirs(inputs, tmp_path / "out")
    assert dirs == [
        tmp_path / "out" / "x",
        tmp_path / "out" / "x (1)",
        tmp_path / "out" / "y (1)",
    ]


def test_future_result_records_failure_and_reraises_broken_pool(tmp_path: Path) -> None:
    crashed: Future[FileResult] = Future()
    crashed.set_exception(RuntimeError("boom"))
    result = _future_result(crashed, tmp_path / "x.xml")
    assert not result.ok
    assert result.error == "RuntimeError: boom"

    poisoned: Future[FileResult] = Future()
    poisoned.set_exception(BrokenProcessPool("pool died"))
    with pytest.raises(BrokenProcessPool):
        _future_result(poisoned, tmp_path / "x.xml")


def test_run_batch_parallel_same_stem_inputs_get_distinct_dirs(tmp_path: Path) -> None:
    a = tmp_path / "a" / "x.xml"
    b = tmp_path / "b" / "x.xml"
    a.parent.mkdir()
    b.parent.mkdir()
    shutil.copy(FIXTURE, a)
    shutil.copy(FIXTURE, b)
    result = run_batch(_granted_template(), [a, b], tmp_path / "out", workers=2, timestamp="t5")
    assert result.succeeded == 2
    assert (tmp_path / "out" / "granted" / "x" / "properties.parquet").is_file()
    assert (tmp_path / "out" / "granted" / "x (1)" / "properties.parquet").is_file()


def test_run_batch_parallel_results_in_input_order(tmp_path: Path) -> None:
    inputs: list[Path] = []
    for name in ("c.xml", "a.xml", "b.xml"):  # deliberately not sorted
        path = tmp_path / name
        shutil.copy(FIXTURE, path)
        inputs.append(path)
    result = run_batch(_granted_template(), inputs, tmp_path / "out", workers=2, timestamp="t6")
    assert [r.source for r in result.results] == [str(p) for p in inputs]


def test_run_batch_should_stop_after_first_file(tmp_path: Path) -> None:
    a = tmp_path / "a.xml"
    b = tmp_path / "b.xml"
    shutil.copy(FIXTURE, a)
    shutil.copy(FIXTURE, b)
    events: list[BatchEvent] = []
    done = 0

    def on_event(event: BatchEvent) -> None:
        nonlocal done
        events.append(event)
        if event.message.startswith(("✓", "✗")):
            done += 1

    result = run_batch(
        _granted_template(),
        [a, b],
        tmp_path / "out",
        timestamp="t7",
        on_event=on_event,
        should_stop=lambda: done >= 1,
    )
    assert result.cancelled is True
    assert (result.succeeded, len(result.results)) == (1, 1)
    assert (tmp_path / "out" / "granted" / "run_t7.log").is_file()  # log still written
    messages = [e.message for e in events]
    assert any(m.startswith("Batch cancelled:") for m in messages)


def test_run_batch_should_stop_immediately_parallel(tmp_path: Path) -> None:
    a = tmp_path / "a.xml"
    b = tmp_path / "b.xml"
    shutil.copy(FIXTURE, a)
    shutil.copy(FIXTURE, b)
    result = run_batch(
        _granted_template(),
        [a, b],
        tmp_path / "out",
        workers=2,
        timestamp="t8",
        should_stop=lambda: True,
    )
    assert result.cancelled is True
    # In-flight files may still finish; only never-started inputs are skipped.
    assert len(result.results) <= 2


def test_needed_tables_skips_flat_when_unused() -> None:
    only_props = BatchTemplate(name="p", steps=[ExportStep(fmt="parquet", tables=["properties"])])
    assert _needed_tables(only_props) == {"properties"}  # flat is skipped at parse
    export_all = BatchTemplate(name="a", steps=[ExportStep(fmt="parquet", tables=None)])
    assert _needed_tables(export_all) is None  # all tables needed


def test_run_batch_normalize_adds_canonical_and_learns(tmp_path: Path) -> None:
    template = BatchTemplate(
        name="norm",
        steps=[
            NormalizeStep(table="assignees", column="name", target="name_canonical", threshold=85),
            ExportStep(fmt="csv", tables=["assignees"]),
        ],
    )
    memory = EntityMemory()
    events: list[BatchEvent] = []
    result = run_batch(
        template, [FIXTURE], tmp_path / "out", memory=memory, timestamp="n1", on_event=events.append
    )
    assert result.succeeded == 1
    header = (
        (tmp_path / "out" / "norm" / "sample_assignment" / "assignees.csv")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert "name_canonical" in header
    assert memory.counts()[0] >= 1  # entity memory learned canonicals from the run
    assert any("normalizing" in e.message for e in events)  # live per-name progress


def test_normalize_step_json_roundtrip(tmp_path: Path) -> None:
    template = BatchTemplate(
        name="n", steps=[NormalizeStep(table="assignors", column="name", threshold=88)]
    )
    path = tmp_path / "t.json"
    dump_templates([template], path)
    step = load_templates(path)[0].steps[0]
    assert isinstance(step, NormalizeStep)
    assert (step.table, step.column, step.threshold) == ("assignors", "name", 88)


def test_template_json_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "templates.json"
    dump_templates([_granted_template()], path)
    loaded = load_templates(path)
    assert len(loaded) == 1
    template = loaded[0]
    assert template.name == "granted"
    assert isinstance(template.steps[0], FilterStep)
    assert template.steps[0].clauses[0].value == "B2"
    assert isinstance(template.steps[1], ExportStep)
    assert template.steps[1].fmt == "parquet"


def test_load_templates_missing_returns_empty(tmp_path: Path) -> None:
    assert load_templates(tmp_path / "none.json") == []


# -- new step behaviors ------------------------------------------------------


def test_normalize_steps_derive_distinct_targets_no_clobber() -> None:
    tables = {
        "assignors": pa.table({"name": ["ACME CORP", "acme corp"]}),
        "assignees": pa.table({"name": ["BETA INC", "beta inc"]}),
    }
    memory = EntityMemory()
    sink = _Sink()
    used: set[tuple[str, str]] = set()
    _apply_normalize(tables, NormalizeStep(table="assignors", column="name"), memory, used, sink)
    _apply_normalize(tables, NormalizeStep(table="assignees", column="name"), memory, used, sink)
    # blank target -> "{column}_canonical"; the two tables get independent canonical columns
    assert "name_canonical" in tables["assignors"].column_names
    assert "name_canonical" in tables["assignees"].column_names
    assert tables["assignors"].column("name_canonical").to_pylist()[0] == "ACME CORP"
    assert tables["assignees"].column("name_canonical").to_pylist()[0] == "BETA INC"


def test_legacy_template_yields_both_canonical_columns() -> None:
    """A pre-fix template (both steps target 'name_canonical' on flat.*_names) must not clobber."""
    legacy: dict[str, Any] = {
        "name": "Norma",
        "load": {},
        "steps": [
            {
                "kind": "normalize",
                "table": "flat",
                "column": "assignor_names",
                "target": "name_canonical",
            },
            {
                "kind": "normalize",
                "table": "flat",
                "column": "assignee_names",
                "target": "name_canonical",
            },
        ],
    }
    template = BatchTemplate.from_dict(legacy)
    tables = {
        "flat": pa.table(
            {
                "assignor_names": ["MACROMEDIA, INC.", "FOO CORP; BAR LLC"],
                "assignee_names": ["ADOBE SYSTEMS INCORPORATED", "BAZ INC"],
            }
        )
    }
    memory = EntityMemory()
    sink = _Sink()
    used: set[tuple[str, str]] = set()
    for step in template.steps:
        assert isinstance(step, NormalizeStep)
        _apply_normalize(tables, step, memory, used, sink)
    flat = tables["flat"]
    # both canonical columns exist, derived per source — no clobber
    assert "assignor_names_canonical" in flat.column_names
    assert "assignee_names_canonical" in flat.column_names
    assert flat.column("assignor_names_canonical").to_pylist()[0] == "MACROMEDIA INC"
    assert flat.column("assignee_names_canonical").to_pylist()[0] == "ADOBE SYSTEMS INCORPORATED"
    # multi-party row was split on "; " and each part normalized then rejoined
    assert flat.column("assignor_names_canonical").to_pylist()[1] == "FOO CORP; BAR LLC"


def test_effective_separator_defaults_for_concatenated_names() -> None:
    assert NormalizeStep(table="flat", column="assignor_names").effective_separator() == "; "
    assert NormalizeStep(table="assignees", column="name").effective_separator() == ""
    # an explicit separator always wins
    assert (
        NormalizeStep(table="flat", column="name", separator=" / ").effective_separator() == " / "
    )


def test_normalize_collision_guard_disambiguates_duplicate_targets() -> None:
    tables = {"t": pa.table({"a": ["ACME"], "b": ["BETA"]})}
    memory = EntityMemory()
    sink = _Sink()
    used: set[tuple[str, str]] = set()
    # two steps on one table forced to the same target -> the second is disambiguated, not clobbered
    _apply_normalize(
        tables, NormalizeStep(table="t", column="a", target="shared"), memory, used, sink
    )
    _apply_normalize(
        tables, NormalizeStep(table="t", column="b", target="shared"), memory, used, sink
    )
    assert "shared" in tables["t"].column_names
    assert "b_canonical" in tables["t"].column_names  # fell back to the 2nd step's derived name
    assert any("already written" in m for m in sink.messages)


def test_apply_dedupe_keeps_first_by_subset() -> None:
    tables = {"t": pa.table({"k": ["a", "a", "b"], "v": ["1", "2", "3"]})}
    sink = _Sink()
    _apply_dedupe(tables, DedupeStep(table="t", subset=["k"]), sink)
    result = tables["t"]
    assert result.num_rows == 2
    assert result.column("v").to_pylist() == ["1", "3"]  # first occurrence per key wins


def test_apply_select_keeps_and_reorders_columns() -> None:
    tables = {"t": pa.table({"a": ["1"], "b": ["2"], "c": ["3"]})}
    _apply_select(tables, SelectStep(table="t", columns=["c", "a"]), _Sink())
    assert tables["t"].column_names == ["c", "a"]


def test_apply_sort_orders_rows() -> None:
    tables = {"t": pa.table({"n": ["3", "1", "2"]})}
    _apply_sort(tables, SortStep(table="t", column="n", ascending=True), _Sink())
    assert tables["t"].column("n").to_pylist() == ["1", "2", "3"]


def test_apply_derive_extracts_year() -> None:
    tables = {"t": pa.table({"recorded_date": ["20240115", "20231231"]})}
    _apply_derive(tables, DeriveStep(table="t", source="recorded_date", op="year"), _Sink())
    assert tables["t"].column("recorded_date_year").to_pylist() == ["2024", "2023"]


def test_apply_aggregate_counts_into_new_sorted_table() -> None:
    tables = {"t": pa.table({"g": ["x", "x", "y"]})}
    sink = _Sink()
    _apply_aggregate(tables, AggregateStep(table="t", group_by=["g"]), sink)
    summary = tables["t_by_g"]
    assert set(summary.column_names) == {"g", "count"}
    rows = dict(
        zip(summary.column("g").to_pylist(), summary.column("count").to_pylist(), strict=True)
    )
    assert rows == {"x": 2, "y": 1}
    assert summary.column("count").to_pylist() == [2, 1]  # sorted by count desc


def test_new_steps_json_roundtrip(tmp_path: Path) -> None:
    template = BatchTemplate(
        name="analysis",
        steps=[
            DedupeStep(table="assignees", subset=["name"]),
            SelectStep(table="assignees", columns=["name"]),
            SortStep(table="assignees", column="name", ascending=False),
            DeriveStep(table="assignments", source="recorded_date", op="year"),
            AggregateStep(table="assignees", group_by=["name"], count_distinct="name"),
        ],
    )
    path = tmp_path / "a.json"
    dump_templates([template], path)
    steps = load_templates(path)[0].steps
    assert [type(s).__name__ for s in steps] == [
        "DedupeStep",
        "SelectStep",
        "SortStep",
        "DeriveStep",
        "AggregateStep",
    ]
    assert isinstance(steps[2], SortStep) and steps[2].ascending is False
    assert isinstance(steps[4], AggregateStep) and steps[4].count_distinct == "name"


def test_apply_classify_labels_entity_types() -> None:
    tables = {"flat": pa.table({"assignor_names": ["MACROMEDIA, INC.", "SMITH, JOHN", "SONY"]})}
    _apply_classify(tables, ClassifyStep(table="flat", column="assignor_names"), _Sink())
    assert tables["flat"].column("assignor_names_type").to_pylist() == [
        "company",
        "individual",
        "unknown",
    ]


def test_apply_compare_flag_and_drop() -> None:
    tables = {
        "flat": pa.table(
            {"a": ["ACME INC", "BETA INC", "ACME INC"], "b": ["ACME INC", "GAMMA", "ACME INC"]}
        )
    }
    _apply_compare(tables, CompareStep(table="flat", left="a", right="b"), _Sink())
    assert tables["flat"].column("a_matches_b").to_pylist() == ["true", "false", "true"]

    dropped = {"flat": tables["flat"]}
    _apply_compare(
        dropped, CompareStep(table="flat", left="a", right="b", action="drop_matches"), _Sink()
    )
    assert dropped["flat"].num_rows == 1  # the two matching rows are removed


def test_apply_compare_fuzzy() -> None:
    tables = {"flat": pa.table({"a": ["ACME CORPORATION"], "b": ["ACME CORP"]})}
    _apply_compare(
        tables,
        CompareStep(table="flat", left="a", right="b", method="fuzzy", threshold=80),
        _Sink(),
    )
    assert tables["flat"].column("a_matches_b").to_pylist() == ["true"]  # fuzzy ≥ 80


def test_apply_transfer_type_keeps_firm_to_firm() -> None:
    tables = {
        "flat": pa.table(
            {
                "assignor_names": ["MACROMEDIA, INC.", "SMITH, JOHN", "ACME INC"],
                "assignee_names": ["ADOBE INC", "ACME INC", "BETA CORP"],
            }
        )
    }
    _apply_transfer_type(tables, TransferTypeStep(), _Sink())  # default company → company
    # only rows where both parties are companies survive (the SMITH individual assignor is dropped)
    assert tables["flat"].column("assignor_names").to_pylist() == ["MACROMEDIA, INC.", "ACME INC"]


def _write_reference(path: Path) -> None:
    path.write_text(
        "disambig_assignee_organization\tassignee_id\n"
        "ADOBE SYSTEMS INCORPORATED\tA1\n"
        "MACROMEDIA INC\tA2\n",
        encoding="utf-8",
    )


def test_apply_reference_match_flags_and_normalizes(tmp_path: Path) -> None:
    ref = tmp_path / "ref.tsv"
    _write_reference(ref)
    tables = {
        "flat": pa.table(
            {"assignor_names": ["Adobe Systems, Inc.", "SMITH, JOHN", "MACROMEDIA INC"]}
        )
    }
    step = ReferenceMatchStep(
        table="flat", column="assignor_names", reference_path=str(ref), id_column="assignee_id"
    )
    _apply_reference_match(tables, step, _Sink())
    flat = tables["flat"]
    assert flat.column("assignor_names_matched").to_pylist() == ["true", "false", "true"]
    assert (
        flat.column("assignor_names_disambiguated").to_pylist()[0] == "ADOBE SYSTEMS INCORPORATED"
    )
    assert flat.column("assignor_names_assignee_id").to_pylist() == ["A1", "", "A2"]


def test_apply_reference_match_keep_matched(tmp_path: Path) -> None:
    ref = tmp_path / "ref.tsv"
    _write_reference(ref)
    tables = {"flat": pa.table({"assignor_names": ["Adobe Systems, Inc.", "SMITH, JOHN"]})}
    step = ReferenceMatchStep(
        table="flat", column="assignor_names", reference_path=str(ref), action="keep_matched"
    )
    _apply_reference_match(tables, step, _Sink())
    assert tables["flat"].num_rows == 1  # the presumed-individual row is dropped


def test_apply_reference_match_missing_file_is_skipped(tmp_path: Path) -> None:
    tables = {"flat": pa.table({"assignor_names": ["ACME INC"]})}
    sink = _Sink()
    _apply_reference_match(
        tables, ReferenceMatchStep(table="flat", column="assignor_names", reference_path=""), sink
    )
    assert "assignor_names_matched" not in tables["flat"].column_names  # unchanged
    assert any("skip reference-match" in m for m in sink.messages)


def test_reference_match_json_roundtrip(tmp_path: Path) -> None:
    template = BatchTemplate(
        name="ref",
        steps=[
            ReferenceMatchStep(
                table="flat",
                column="assignor_names",
                reference_path="ref.tsv",
                id_column="assignee_id",
                scorer="token_set",
                mode="all",
                action="keep_matched",
            )
        ],
    )
    path = tmp_path / "ref.json"
    dump_templates([template], path)
    step = load_templates(path)[0].steps[0]
    assert isinstance(step, ReferenceMatchStep)
    assert (step.reference_path, step.id_column, step.scorer, step.mode, step.action) == (
        "ref.tsv",
        "assignee_id",
        "token_set",
        "all",
        "keep_matched",
    )


def test_classify_compare_transfer_json_roundtrip(tmp_path: Path) -> None:
    template = BatchTemplate(
        name="rel",
        steps=[
            ClassifyStep(table="flat", column="assignor_names", method="rules", mode="any"),
            CompareStep(table="flat", left="a", right="b", method="fuzzy", action="drop_matches"),
            TransferTypeStep(assignor_type="individual", assignee_type="company"),
        ],
    )
    path = tmp_path / "rel.json"
    dump_templates([template], path)
    steps = load_templates(path)[0].steps
    assert [type(s).__name__ for s in steps] == [
        "ClassifyStep",
        "CompareStep",
        "TransferTypeStep",
    ]
    assert isinstance(steps[0], ClassifyStep) and steps[0].mode == "any"
    assert isinstance(steps[1], CompareStep) and steps[1].action == "drop_matches"
    assert isinstance(steps[2], TransferTypeStep) and steps[2].assignor_type == "individual"


def test_needed_tables_includes_new_step_tables() -> None:
    template = BatchTemplate(
        name="a",
        steps=[
            DeriveStep(table="assignments", source="recorded_date", op="year"),
            AggregateStep(table="assignees", group_by=["name"]),
            ExportStep(fmt="parquet", tables=["assignees"]),
        ],
    )
    assert _needed_tables(template) == {"assignments", "assignees"}


def test_every_step_kind_survives_a_zeroed_table(tmp_path: Path) -> None:
    """Regression for the pyarrow-25 empty-kernel SIGSEGV: a zeroed table must pass every step."""
    store = parse_to_store(FIXTURE, tmp_path, tables={"flat"})
    flat = store.tables["flat"]
    empty = flat.take(filter_sort(flat, [FilterClause("assignor_names", "equals", "___none___")]))
    assert empty.num_rows == 0
    steps: list[BatchStep] = [
        FilterStep(table="flat", clauses=[FilterClause("assignee_names", "equals", "x")]),
        NormalizeStep(table="flat", column="assignor_names"),
        ClassifyStep(table="flat", column="assignee_names"),
        CompareStep(table="flat", left="assignor_names", right="assignee_names", method="exact"),
        CompareStep(table="flat", left="assignor_names", right="assignee_names", method="fuzzy"),
        TransferTypeStep(),
        DedupeStep(table="flat", subset=["reel_no"]),
        SelectStep(table="flat", columns=["reel_no"]),
        SortStep(table="flat", column="reel_no"),
        DeriveStep(table="flat", source="recorded_date", op="year"),
        AggregateStep(table="flat", group_by=["reel_no"]),
        ExportStep(fmt="csv", tables=["flat"]),
    ]
    for step in steps:  # each step gets a fresh zeroed working set; none may crash
        tables = {"flat": empty}
        _apply_step(tables, step, EntityMemory(), set(), tmp_path, lambda e: None)
        assert tables["flat"].num_rows == 0


def test_apply_export_empty_tables_writes_nothing(tmp_path: Path) -> None:
    template = BatchTemplate(name="nothing", steps=[ExportStep(fmt="csv", tables=[])])
    events: list[BatchEvent] = []
    result = run_batch(
        template, [FIXTURE], tmp_path / "out", timestamp="t9", on_event=events.append
    )
    assert result.succeeded == 1
    out_dir = tmp_path / "out" / "nothing" / "sample_assignment"
    assert list(out_dir.glob("*.csv")) == []  # [] is "nothing", not "everything"
    assert any("no tables selected" in e.message for e in events)


def test_normalize_step_score_review_roundtrip_and_schema(tmp_path: Path) -> None:
    step = NormalizeStep(
        table="flat", column="assignor_names", emit_score=True, review_threshold=95
    )
    template = BatchTemplate(name="conf", steps=[step])
    path = tmp_path / "t.json"
    dump_templates([template], path)
    loaded = load_templates(path)[0].steps[0]
    assert isinstance(loaded, NormalizeStep)
    assert loaded.emit_score is True and loaded.review_threshold == 95

    cols = columns_after(LoadConfig(), [step], upto=1)["flat"]
    assert "assignor_names_canonical_score" in cols
    assert "assignor_names_canonical_review" in cols
    # legacy template without the new keys decodes with defaults
    dump_templates([BatchTemplate(name="old", steps=[NormalizeStep(table="flat")])], path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    for key in ("emit_score", "review_threshold"):
        raw[0]["steps"][0].pop(key, None)
    path.write_text(json.dumps(raw), encoding="utf-8")
    old = load_templates(path)[0].steps[0]
    assert isinstance(old, NormalizeStep)
    assert old.emit_score is False and old.review_threshold == 0


def test_run_batch_normalize_emits_score_and_review_columns(tmp_path: Path) -> None:
    template = BatchTemplate(
        name="conf",
        steps=[
            NormalizeStep(
                table="assignees", column="name", emit_score=True, review_threshold=101
            ),  # review_threshold 101: every fuzzy accept lands in the band (test determinism)
            ExportStep(fmt="csv", tables=["assignees"]),
        ],
    )
    run_batch(template, [FIXTURE], tmp_path / "out", timestamp="conf")
    header = (
        (tmp_path / "out" / "conf" / "sample_assignment" / "assignees.csv")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert "name_canonical_score" in header
    assert "name_canonical_review" in header


def test_apply_compare_fuzzy_score_and_review_columns() -> None:
    table = pa.table(
        {
            "a": ["ACME CORPORATION", "ACME CORPORATION", "ACME CORPORATION", None],
            "b": ["ACME CORPORATION", "ACME CORPORATON", "ZZZZZ", "X"],
        }
    )
    step = CompareStep(
        table="t",
        left="a",
        right="b",
        method="fuzzy",
        threshold=85,
        emit_score=True,
        review_threshold=100,
    )
    tables = {"t": table}
    _apply_compare(tables, step, _Sink())
    result = tables["t"]
    scores = result.column("a_matches_b_score").to_pylist()
    review = result.column("a_matches_b_review").to_pylist()
    flags = result.column("a_matches_b").to_pylist()
    assert scores[0] == 100 and flags[0] == "true" and review[0] == "false"  # exact never reviews
    assert scores[1] is not None and 85 <= scores[1] < 100  # fuzzy match -> review band
    assert flags[1] == "true" and review[1] == "true"
    assert flags[2] == "false" and review[2] == "false"  # below threshold: no match, no review
    assert scores[3] is None and flags[3] == "false"  # null comparand


def test_apply_compare_exact_keeps_threshold_free_semantics() -> None:
    table = pa.table({"a": ["X", "Y"], "b": ["X", "Z"]})
    step = CompareStep(table="t", left="a", right="b", method="exact", threshold=0, emit_score=True)
    tables = {"t": table}
    _apply_compare(tables, step, _Sink())
    assert tables["t"].column("a_matches_b").to_pylist() == ["true", "false"]
    assert tables["t"].column("a_matches_b_score").to_pylist() == [100, 0]


def test_compare_step_score_review_roundtrip_and_schema(tmp_path: Path) -> None:
    step = CompareStep(
        table="flat",
        left="x",
        right="y",
        emit_score=True,
        review_threshold=92,
        action="keep_matches",
    )
    path = tmp_path / "t.json"
    dump_templates([BatchTemplate(name="c", steps=[step])], path)
    loaded = load_templates(path)[0].steps[0]
    assert isinstance(loaded, CompareStep)
    assert loaded.emit_score is True and loaded.review_threshold == 92
    cols = columns_after(LoadConfig(), [step], upto=1)["flat"]
    assert "x_matches_y_score" in cols and "x_matches_y_review" in cols
    assert "x_matches_y" not in cols  # non-flag action adds no boolean column


def test_validate_template_flags_reference_column_mismatch(tmp_path: Path) -> None:
    noid = tmp_path / "reference.parquet"
    pq.write_table(pa.table({"organization": ["ACME CORPORATION"]}), noid)  # pyright: ignore[reportUnknownMemberType]
    step = ReferenceMatchStep(
        table="flat",
        column="assignor_names",
        reference_path=str(noid),
        name_column="organization",
        id_column="assignee_id",  # not in the file -> pre-run warning, not a runtime KeyError
    )
    warnings = validate_template(LoadConfig(), [step])
    assert any("no column 'assignee_id'" in w and "organization" in w for w in warnings)
    # with the id column cleared the template validates clean
    step.id_column = ""
    assert not any("no column" in w for w in validate_template(LoadConfig(), [step]))


def test_describe_step_one_line_per_kind() -> None:
    clause = FilterClause("doc_kind", "equals", "B2")
    described = describe_step(
        FilterStep(table="flat", clauses=[clause, clause, clause], combine="and")
    )
    assert described == "Filter · flat · 3 clause(s) · AND"
    assert "review<95" in describe_step(
        NormalizeStep(table="flat", column="name", emit_score=True, review_threshold=95)
    )
    steps: list[BatchStep] = [
        DedupeStep(table="flat"),
        SelectStep(table="flat", columns=["a"]),
        SortStep(table="flat", column="a"),
        DeriveStep(table="flat", source="recorded_date", op="year"),
        AggregateStep(table="flat", group_by=["a"]),
        ClassifyStep(table="flat", column="name"),
        CompareStep(table="flat", left="a", right="b"),
        TransferTypeStep(),
        ReferenceMatchStep(table="flat"),
        ExportStep(fmt="csv"),
    ]
    for step in steps:
        assert describe_step(step)  # every kind renders a non-empty one-liner
