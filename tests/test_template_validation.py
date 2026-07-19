"""Regression tests: template decode hardening, enum validation, and run-time guards."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as _pq
import pytest

from uspto_assignments import (
    BatchStep,
    BatchTemplate,
    CompareStep,
    CpcMatchStep,
    CpcRunContext,
    ExportStep,
    FilterStep,
    LoadConfig,
    TemplateFormatError,
    filters,
    load_templates,
    run_batch,
    validate_template,
)
from uspto_assignments.batch import BatchEvent, _apply_compare, _apply_export, inputs_schema_base
from uspto_assignments.filters import FilterClause
from uspto_assignments.tables import dataset_columns

# pyarrow.parquet is under-typed in pyarrow-stubs; route through Any (see filters.py).
pq: Any = _pq


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "t.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _template(steps: list[dict[str, object]]) -> list[dict[str, object]]:
    return [{"name": "t", "load": {}, "steps": steps}]


# ---------------------------------------------------------------- decode hardening
def test_load_templates_rejects_non_json_with_named_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(TemplateFormatError, match=r"bad\.json"):
        load_templates(path)


def test_load_templates_rejects_top_level_object(tmp_path: Path) -> None:
    with pytest.raises(TemplateFormatError, match="JSON array"):
        load_templates(_write(tmp_path, {"name": "x", "steps": []}))


def test_load_templates_rejects_unknown_step_kind(tmp_path: Path) -> None:
    """Regression: a typo'd kind used to silently decode as a no-op FilterStep."""
    path = _write(tmp_path, _template([{"kind": "normalise", "table": "flat"}]))
    with pytest.raises(TemplateFormatError, match="unknown step kind 'normalise'"):
        load_templates(path)


def test_load_templates_names_template_and_step_in_errors(tmp_path: Path) -> None:
    path = _write(tmp_path, _template([{"kind": "export"}, {"kind": "derive", "table": "flat"}]))
    with pytest.raises(TemplateFormatError, match=r"template 1 \('t'\): step 2"):
        load_templates(path)  # derive is missing its required 'source'


@pytest.mark.parametrize(
    ("step", "match"),
    [
        ({"kind": "filter", "table": "flat", "combine": "AND"}, "invalid combine"),
        ({"kind": "export", "fmt": "excel"}, "invalid fmt"),
        ({"kind": "derive", "table": "flat", "source": "d", "op": "yr"}, "invalid op"),
        (
            {"kind": "compare", "table": "flat", "left": "a", "right": "b", "action": "drop"},
            "invalid action",
        ),
        (
            {"kind": "normalize", "table": "flat", "column": "name", "scorer": "token_sett"},
            "invalid scorer",
        ),
        (
            {"kind": "normalize", "table": "flat", "column": "name", "threshold": 150},
            "between 0 and 100",
        ),
        (
            {"kind": "filter", "table": "flat", "enabled": "false"},
            "must be true or false",
        ),
        (
            {"kind": "filter", "table": "flat", "clauses": [{"column": "a", "op": "=="}]},
            "invalid op",
        ),
        (
            {"kind": "filter", "table": "flat", "sort": "recorded_date"},
            "sort must be",
        ),
        (
            {"kind": "cpc_match", "portfolio_mode": "footprint"},
            "invalid portfolio_mode",
        ),
        (
            {"kind": "transfer_type", "assignor_type": "Company"},
            "invalid assignor_type",
        ),
    ],
)
def test_enum_and_shape_typos_fail_import(
    tmp_path: Path, step: dict[str, object], match: str
) -> None:
    """Regression: these all imported cleanly and ran with silently wrong semantics."""
    with pytest.raises(TemplateFormatError, match=match):
        load_templates(_write(tmp_path, _template([step])))


def test_load_limit_zero_or_negative_means_all() -> None:
    """Regression: limit 0/-5 parsed exactly ONE record; the UI treats 0 as 'all'."""
    assert LoadConfig.from_dict({"limit": 0}).limit is None
    assert LoadConfig.from_dict({"limit": -5}).limit is None
    assert LoadConfig.from_dict({"limit": 100}).limit == 100


# ---------------------------------------------------------------- validation warnings
def test_validate_warns_on_in_range_without_upper_bound() -> None:
    step = FilterStep(table="flat", clauses=[FilterClause("recorded_date", "in_range", "20200101")])
    warnings = validate_template(LoadConfig(), [step])
    assert any("no upper bound" in w for w in warnings)


def test_validate_warns_on_export_of_unknown_table() -> None:
    warnings = validate_template(LoadConfig(), [ExportStep(fmt="csv", tables=["flatt"])])
    assert any("'flatt' does not exist" in w for w in warnings)


def test_validate_warns_on_load_table_with_no_columns() -> None:
    warnings = validate_template(
        LoadConfig(columns={"flat": []}), [ExportStep(fmt="csv", tables=["flat"])]
    )
    assert any("selects no columns" in w for w in warnings)


# ---------------------------------------------------------------- runtime guards
def test_filter_apply_rejects_unknown_op_and_combine() -> None:
    table = pa.table({"a": ["x", "y"]})
    bad_op = FilterClause("a", "==", "x")  # type: ignore[arg-type]  # runtime data can't be Literal-checked
    with pytest.raises(ValueError, match="unknown filter op"):
        filters.apply(table, [bad_op])
    good = FilterClause("a", "equals", "x")
    with pytest.raises(ValueError, match="unknown combine mode"):
        filters.apply(table, [good], combine="AND")  # type: ignore[arg-type]


def test_compare_two_empty_values_are_not_a_match() -> None:
    """Regression: '' == '' counted as a match — drop_matches nuked doubly-unresolved id rows."""
    tables = {
        "flat": pa.table(
            {
                "left_id": ["A1", "", "A2"],
                "right_id": ["A1", "", "B9"],
            }
        )
    }
    step = CompareStep(
        table="flat", left="left_id", right="right_id", method="exact", action="drop_matches"
    )
    _apply_compare(tables, step, lambda _e: None)
    # Row 0 (real self-match) drops; row 1 (both unresolved) and row 2 (different ids) survive.
    assert tables["flat"].column("left_id").to_pylist() == ["", "A2"]


def test_export_missing_table_emits_warning_event(tmp_path: Path) -> None:
    events: list[BatchEvent] = []
    step = ExportStep(fmt="csv", tables=["flatt"])
    written = _apply_export({"flat": pa.table({"a": ["1"]})}, step, tmp_path, events.append)
    assert written == []
    assert any(e.level == "warning" and "'flatt'" in e.message for e in events)


# ---------------------------------------------------------------- round-trip of shipped templates
def test_all_shipped_templates_still_import_and_round_trip() -> None:
    tdir = Path(__file__).parent.parent / "templates"
    for path in sorted(tdir.glob("*.json")):
        templates = load_templates(path)
        assert templates, path.name
        for template in templates:
            again = BatchTemplate.from_dict(template.to_dict())
            assert again.to_dict() == template.to_dict(), path.name


# ---------------------------------------------------------------- dataset-input schema awareness
def _processed_dataset(tmp_path: Path) -> Path:
    """A minimal '13-style' processed dataset: flat.parquet with canonical + CPC columns."""
    dataset = tmp_path / "processed"
    dataset.mkdir()
    flat = pa.table(
        {
            "reel_no": ["000001"],
            "frame_no": ["0001"],
            "transaction_date": ["20240101"],
            "assignee_names_canonical": ["ACME CORP"],
            "doc_number": ["10000001"],
            "doc_kind": ["B2"],
            "cpc_codes": pa.array([["H04L9/32"]], type=pa.list_(pa.string())),
        }
    )
    pq.write_table(flat, dataset / "flat.parquet")
    return dataset


def test_dataset_columns_reads_schema_without_loading(tmp_path: Path) -> None:
    dataset = _processed_dataset(tmp_path)
    columns = dataset_columns(dataset)
    assert "cpc_codes" in columns["flat"]
    assert "assignee_names_canonical" in columns["flat"]
    assert dataset_columns(tmp_path) == {}  # not a dataset dir


def test_cpc_match_on_dataset_input_validates_clean_and_runs_strict(tmp_path: Path) -> None:
    """Regression: the documented 13→14 chain always warned 'column not available yet' and
    strict mode refused to run it — validation now reads the dataset input's real schema."""
    dataset = _processed_dataset(tmp_path)
    footprint = tmp_path / "footprint.csv"
    footprint.write_text("patent,cpc\n9000001,H04L9/32\n", encoding="utf-8")
    steps: list[BatchStep] = [
        CpcMatchStep(table="flat", portfolio_mode="footprint_file", portfolio_path=str(footprint)),
        ExportStep(fmt="csv", tables=["matched_buyers_overall"]),
    ]
    base = inputs_schema_base([dataset])
    assert base is not None and "cpc_codes" in base["flat"]
    assert validate_template(LoadConfig(), steps, base=base) == []
    # Without the base, the static schema still (correctly) warns — raw-parse inputs lack CPC.
    assert any("cpc_codes" in w for w in validate_template(LoadConfig(), steps))

    result = run_batch(
        BatchTemplate(name="chain", steps=steps),
        [dataset],
        tmp_path / "out",
        timestamp="t",
        cpc_ctx=CpcRunContext(),
        strict=True,  # previously raised TemplateValidationError on this exact workflow
    )
    assert result.succeeded == 1 and result.warnings == []
