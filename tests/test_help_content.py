"""Tests for the batch-dialog Help panel's content (uspto_assignments_ui.help_content).

Pure content/HTML generation — no Qt/QApplication needed.
"""

from __future__ import annotations

import json
from pathlib import Path

from uspto_assignments import (
    AggregateStep,
    AttachCpcFileStep,
    BatchTemplate,
    ClassifyStep,
    CompareStep,
    CpcMatchStep,
    DedupeStep,
    DeriveStep,
    ExportStep,
    FetchCpcStep,
    FilterStep,
    NormalizeStep,
    ReferenceMatchStep,
    SelectStep,
    SortStep,
    TransferTypeStep,
    load_templates,
)
from uspto_assignments.filters import FilterClause
from uspto_assignments_ui.help_content import (
    _STEP_HELP,
    _TEMPLATE_HELP,
    step_help_html,
    template_help_html,
    welcome_help_html,
)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
_PRESET_NAMES = {"Firm-to-firm transfers", "Top assignees", "Enrich flat (names + types)"}

# One representative instance per step kind, used to exercise step_help_html/_effect.
_SAMPLE_STEPS = [
    FilterStep(table="flat", clauses=[FilterClause("conveyance_text", "contains", "ASSIGN")]),
    FilterStep(table="flat", columns=["reel_no", "frame_no"]),
    NormalizeStep(table="flat", column="assignor_names"),
    ClassifyStep(table="flat", column="assignee_names"),
    CompareStep(table="flat", left="a_canonical", right="b_canonical"),
    TransferTypeStep(table="flat"),
    ReferenceMatchStep(table="flat", column="assignor_names", reference_path="ref.parquet"),
    FetchCpcStep(table="flat", column="doc_number"),
    AttachCpcFileStep(table="flat", column="doc_number", source_path="cpc.csv"),
    CpcMatchStep(table="flat", portfolio_path="portfolio.txt"),
    DedupeStep(table="flat", subset=["reel_no", "frame_no"]),
    SelectStep(table="flat", columns=["reel_no", "frame_no"]),
    SortStep(table="flat", column="recorded_date"),
    DeriveStep(table="flat", source="transaction_date", op="year"),
    AggregateStep(table="flat", group_by=["assignee_names"]),
    ExportStep(fmt="parquet", tables=["flat"]),
]


def _shipped_and_preset_names() -> set[str]:
    names: set[str] = set(_PRESET_NAMES)
    for path in sorted(TEMPLATES_DIR.glob("*.json")):
        for template in json.loads(path.read_text(encoding="utf-8")):
            names.add(template["name"])
    return names


def test_every_shipped_template_and_preset_has_curated_help() -> None:
    """Regression: a renamed/added template must not silently fall back to the generic view."""
    missing = _shipped_and_preset_names() - set(_TEMPLATE_HELP)
    assert not missing, f"no curated help for: {sorted(missing)}"


def test_no_stale_curated_entries_for_removed_templates() -> None:
    """Catches a curated entry left behind after a template was renamed or deleted."""
    stale = set(_TEMPLATE_HELP) - _shipped_and_preset_names()
    assert not stale, f"curated help for templates that no longer exist: {sorted(stale)}"


def _produces_anchors(template: BatchTemplate) -> tuple[set[str], set[str]]:
    """(output table names, export format words) a template actually writes."""
    tables: set[str] = set()
    formats: set[str] = set()
    for step in template.steps:
        if isinstance(step, AggregateStep):
            tables.add(step.resolved_out())
        elif isinstance(step, CpcMatchStep):
            tables.update({step.out_table, step.overall_table})
            if step.emit_class_matches:
                tables.add(step.class_match_table)
        elif isinstance(step, ExportStep):
            formats.add(step.fmt)
            if step.tables:  # explicit table list (None = "all", too generic to anchor on)
                tables.update(step.tables)
    return tables, formats


def _needs_anchors(template: BatchTemplate) -> set[str]:
    """Basenames of the external files a template requires at run time."""
    files: set[str] = set()
    for step in template.steps:
        for attr in ("reference_path", "source_path", "portfolio_path"):
            path = getattr(step, attr, "")
            if path:
                files.add(Path(path).name)
    return files


def test_curated_produces_and_needs_match_real_template_structure() -> None:
    """Regression: the curated Produces:/Needs: prose must NAME the real output tables and required
    files — so a template edit that renames an output or swaps a reference file can't leave the help
    silently wrong (the old presence-only test could not catch that)."""
    problems: list[str] = []
    for path in sorted(TEMPLATES_DIR.glob("*.json")):
        for template in load_templates(path):
            info = _TEMPLATE_HELP.get(template.name)
            if info is None:
                continue  # presence is covered by the coverage test
            produces = info.produces.lower()
            tables, formats = _produces_anchors(template)
            for name in tables:
                if name.lower() not in produces:
                    problems.append(f"{template.name!r}: Produces omits output table {name!r}")
            if formats and not any(fmt.lower() in produces for fmt in formats):
                problems.append(f"{template.name!r}: Produces omits the export format {formats}")
            needs = info.needs.lower()
            for basename in _needs_anchors(template):
                if basename.lower() not in needs:
                    problems.append(f"{template.name!r}: Needs omits required file {basename!r}")
    assert not problems, "curated help drifted from template structure:\n" + "\n".join(problems)


def test_every_step_kind_has_help() -> None:
    kinds = {type(step) for step in _SAMPLE_STEPS}
    assert set(_STEP_HELP) == kinds


def test_step_help_html_includes_title_live_summary_and_tip() -> None:
    step = NormalizeStep(table="flat", column="assignor_names", scorer="token_set")
    out = step_help_html(step)
    assert "<h3>Normalize step</h3>" in out
    assert "assignor_names_canonical" in out  # the computed "On this step" line
    assert "assignor_names" in out  # the live describe_step() one-liner
    assert "<b>Tip:</b>" in out


def test_step_help_html_flags_disabled_step() -> None:
    step = SortStep(table="flat", column="recorded_date")
    step.enabled = False
    out = step_help_html(step)
    assert "Disabled" in out


def test_select_step_effect_lists_kept_columns_not_a_diff() -> None:
    step = SelectStep(table="flat", columns=["reel_no", "frame_no"])
    out = step_help_html(step)
    assert "keeps 2 column(s): reel_no, frame_no" in out


def test_filter_step_with_columns_reports_projection_and_row_filter() -> None:
    step = FilterStep(
        table="flat",
        clauses=[FilterClause("reel_no", "not_empty")],
        columns=["reel_no"],
    )
    out = step_help_html(step)
    assert "projects to 1 column(s): reel_no" in out
    assert "filters rows (1 clause(s), AND)" in out


def test_filter_step_reports_row_filter_not_no_new_columns() -> None:
    """Regression: a filter used to report the misleading 'no new columns'."""
    step = FilterStep(table="flat", clauses=[FilterClause("reel_no", "not_empty")])
    out = step_help_html(step)
    assert "filters rows (1 clause(s), AND)" in out
    assert "no new columns" not in out


def test_row_effect_phrasing_per_kind() -> None:
    cases = [
        (
            DedupeStep(table="flat", subset=["reel_no", "frame_no"]),
            "removes duplicate rows (key: reel_no, frame_no)",
        ),
        (TransferTypeStep(table="flat"), "keeps only company → company rows"),
        (
            CompareStep(table="flat", left="a", right="b", action="drop_matches"),
            "drops rows where a matches b",
        ),
        (
            CompareStep(table="flat", left="a", right="b", action="keep_matches"),
            "keeps rows where a matches b",
        ),
        (SortStep(table="flat", column="recorded_date"), "reorders rows by recorded_date"),
    ]
    for step, expected in cases:
        assert expected in step_help_html(step), expected


def test_reference_match_reports_columns_and_row_filter() -> None:
    step = ReferenceMatchStep(
        table="flat", column="assignor_names", reference_path="ref.parquet", action="keep_matched"
    )
    out = step_help_html(step)
    assert "assignor_names_disambiguated" in out  # columns it adds
    assert "keeps only matched rows" in out  # and the row filter its action applies


def test_compare_flag_action_has_no_row_effect() -> None:
    step = CompareStep(table="flat", left="a", right="b", action="flag")
    out = step_help_html(step)
    assert "drops rows" not in out and "keeps rows" not in out


def test_export_step_effect_names_tables_and_format() -> None:
    assert "writes flat as parquet" in step_help_html(ExportStep(fmt="parquet", tables=["flat"]))
    assert "writes all tables as csv" in step_help_html(ExportStep(fmt="csv", tables=None))


def test_aggregate_step_effect_reports_new_table() -> None:
    step = AggregateStep(table="flat", group_by=["assignee_names_canonical"])
    out = step_help_html(step)
    assert "new table" in out
    assert "flat_by_assignee_names_canonical" in out
    assert "assignee_names_canonical, count" in out


def test_cpc_match_step_effect_reports_all_output_tables() -> None:
    step = CpcMatchStep(table="flat", portfolio_path="portfolio.txt", emit_class_matches=True)
    out = step_help_html(step)
    assert "matched_buyers_by_portfolio_patent" in out
    assert "matched_buyers_overall" in out
    assert "matched_cpc_classes" in out
    assert out.count("new table") == 3


def test_every_sample_step_renders_without_error() -> None:
    for step in _SAMPLE_STEPS:
        html = step_help_html(step)
        assert html.startswith("<h3>")
        assert len(html) > 0


def test_template_help_uses_curated_summary_for_known_template() -> None:
    out = template_help_html("12 - Convert to Parquet (all tables)", [ExportStep(fmt="parquet")])
    assert "<h3>12 - Convert to Parquet (all tables)</h3>" in out
    assert "Produces:" in out
    assert "No built-in description" not in out


def test_template_help_falls_back_to_pipeline_listing_for_unknown_template() -> None:
    steps = [
        FilterStep(table="flat"),
        NormalizeStep(table="flat", column="assignor_names"),
    ]
    out = template_help_html("My custom pipeline", steps)
    assert "<h3>My custom pipeline</h3>" in out
    assert "No built-in description" in out
    assert "Pipeline</b> (2 step(s))" in out
    assert "<ol>" in out and out.count("<li>") == 2


def test_template_help_marks_disabled_steps_in_pipeline_listing() -> None:
    disabled = NormalizeStep(table="flat", column="assignor_names")
    disabled.enabled = False
    out = template_help_html("Unknown", [disabled])
    assert "disabled" in out


def test_template_help_handles_empty_pipeline() -> None:
    out = template_help_html("Empty template", [])
    assert "No steps yet" in out


def test_template_help_escapes_untrusted_name() -> None:
    out = template_help_html("<script>alert(1)</script>", [])
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_embedded_description_wins_over_curated_help() -> None:
    """A template that carries its own description documents itself, even over a curated entry."""
    out = template_help_html(
        "12 - Convert to Parquet (all tables)",
        [ExportStep(fmt="parquet")],
        description="My own words for this template.",
    )
    assert "My own words for this template." in out
    assert "Produces:" not in out  # the curated summary is not used when an embedded one exists


def test_embedded_description_used_for_otherwise_unknown_template() -> None:
    out = template_help_html(
        "Some imported template", [ExportStep(fmt="csv")], description="Does X."
    )
    assert "Does X." in out
    assert "No built-in description" not in out


def test_blank_description_falls_back_to_curated_then_generic() -> None:
    curated = template_help_html("12 - Convert to Parquet (all tables)", [], description="   ")
    assert "Produces:" in curated  # blank description → curated help still used
    generic = template_help_html("Unknown template", [ExportStep(fmt="csv")], description="")
    assert "No built-in description" in generic


def test_embedded_description_is_escaped() -> None:
    out = template_help_html("t", [], description="<b>x</b>")
    assert "<b>x</b>" not in out
    assert "&lt;b&gt;x&lt;/b&gt;" in out


def test_welcome_help_html_is_non_empty() -> None:
    out = welcome_help_html()
    assert "Help" in out
    assert "Select a saved template" in out
