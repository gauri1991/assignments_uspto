"""Curated help text for the batch dialog's Help panel.

Two kinds of content:

- **Step help** is generic per step *kind* (what a Normalize step does, in general) plus a
  live, instance-specific summary of exactly what THIS step is configured to do — computed
  from the same schema engine :func:`~uspto_assignments.columns_after` that drives validation,
  so it can never drift from the real behavior.
- **Template help** is a curated one-paragraph description for every template this project
  ships (looked up by exact name), with a generic fallback — a numbered pipeline listing built
  from :func:`~uspto_assignments.describe_step` — for presets, imports, or user-authored
  templates that have no curated entry.

Pure text/HTML generation, no Qt — importable and testable without a QApplication.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from dataclasses import dataclass

from uspto_assignments import (
    AggregateStep,
    AttachCpcFileStep,
    BatchStep,
    ClassifyStep,
    CompareStep,
    CpcMatchStep,
    DedupeStep,
    DeriveStep,
    ExportStep,
    FetchCpcStep,
    FilterStep,
    KindFilterStep,
    LoadConfig,
    NormalizeStep,
    ReferenceMatchStep,
    SelectStep,
    SortStep,
    TransferTypeStep,
    columns_after,
    describe_step,
)

_MAX_LISTED_COLUMNS = 10  # cap inline column lists so a 9-column cpc_match table stays crisp


# --------------------------------------------------------------------------------------
# Step help — one entry per step *kind*
# --------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _StepHelp:
    """Static, kind-level help text for one step kind."""

    title: str
    what: str
    tip: str = ""


_STEP_HELP: dict[type[BatchStep], _StepHelp] = {
    FilterStep: _StepHelp(
        title="Filter",
        what=(
            "Keeps only the rows matching your clauses (contains / equals / starts with / "
            "in range / empty checks). Clauses combine with AND (all must match) or OR (any "
            "must match)."
        ),
        tip="Filters are exact string matches — for fuzzy company-name matching, use "
        "Normalize, Compare, or Reference match instead.",
    ),
    NormalizeStep: _StepHelp(
        title="Normalize",
        what=(
            "Fuzzy-cleans a name column to one canonical form using a shared, learned "
            "“entity memory” — e.g. “ACME INC.” and “Acme "
            "Incorporated” collapse to one name."
        ),
        tip="“Learn new canonicals” (default on) grows the shared memory across "
        "every run, so results are path-dependent — turn it off for a reproducible run.",
    ),
    ClassifyStep: _StepHelp(
        title="Classify",
        what=(
            "Labels a name column as company / individual / unknown, using simple rules "
            "(legal suffixes, the “LAST, FIRST” person pattern) or an optional ML "
            "method."
        ),
        tip="Multi-party values are combined via the mode setting: “all” needs "
        "every party to agree, else the result is “unknown”.",
    ),
    CompareStep: _StepHelp(
        title="Compare columns",
        what=(
            "Compares two columns row by row — exact equality or fuzzy similarity — and "
            "flags matches, or drops/keeps matching rows. The classic use: remove "
            "self-transfers where the buyer and seller are the same entity."
        ),
        tip="Two blank values never count as a match — an id that failed to resolve on "
        "both sides isn't “the same entity”.",
    ),
    TransferTypeStep: _StepHelp(
        title="Transfer type",
        what=(
            "A one-step preset that keeps only rows where the assignor and assignee are "
            "each a chosen entity type — the default (company → company) keeps "
            "firm-to-firm deals only."
        ),
        tip="Classifies fresh every run (rules or ML) — it doesn't check a reference "
        "gazetteer the way Reference match does.",
    ),
    KindFilterStep: _StepHelp(
        title="Kind code filter",
        what=(
            "Keeps or discards rows by document kind — pick document types "
            "(Grant / Application / Publication / Unknown, classified robustly so Application "
            "covers X0 serials) and/or exact kind codes (B1, B2, A1, X0, …)."
        ),
        tip="Grant = B1+B2. Applications split into A1 (publication) and X0 (serial), so tick "
        "both Application and Publication for every non-grant, or use exact codes for precision.",
    ),
    ReferenceMatchStep: _StepHelp(
        title="Reference match",
        what=(
            "Matches a name column against a disambiguated-assignee reference file (e.g. "
            "PatentsView's gazetteer) to confirm known companies and capture their "
            "canonical name/id."
        ),
        tip="“Keep only matched” is the strict gate (only confirmed companies "
        "survive); “Flag” keeps every row and just marks matches for a later "
        "filter.",
    ),
    FetchCpcStep: _StepHelp(
        title="Fetch CPC codes",
        what=(
            "Attaches CPC classification codes to a granted-patent column, using the "
            "project's CPC data source (Settings ▸ CPC data source), cache-first. CPC is "
            "grant-only — non-grant rows are skipped."
        ),
        tip="Offline by default — uncached patents are only fetched when the run's "
        "“Allow network” is on.",
    ),
    AttachCpcFileStep: _StepHelp(
        title="Attach CPC from file",
        what=(
            "The same CPC columns as Fetch CPC codes, but joined from a file you supply "
            "(e.g. a PatSeer export) — fully offline, no API, no cache. Grant-only."
        ),
        tip="The file's patent ids can be bare grant numbers or publication-style "
        "(US10987654B2) — both normalize and join correctly. A PatSeer cell often packs "
        "several CPCs; set the separator (e.g. “;”) to split them.",
    ),
    CpcMatchStep: _StepHelp(
        title="CPC match to portfolio",
        what=(
            "Ranks buyers by how much their CPC footprint overlaps a sales-package "
            "(portfolio) of patents — for finding the most technologically-relevant "
            "buyer for a portfolio."
        ),
        tip="Run Fetch CPC codes / Attach CPC from file first — this step reads the "
        "cpc_codes they attach. Ranking knobs (grain, metric, weights) come from "
        "Settings ▸ CPC data source, not this step.",
    ),
    DedupeStep: _StepHelp(
        title="Deduplicate",
        what="Removes duplicate rows, keeping the first occurrence of each key (or the "
        "whole row if no key is set).",
        tip="To count distinct deals rather than distinct patents, dedupe on reel_no, "
        "frame_no (the assignment record's own key) before aggregating.",
    ),
    SelectStep: _StepHelp(
        title="Select columns",
        what="Keeps only the columns you choose, in the order you choose them, and drops the rest.",
        tip="Use this to slim a wide table before an expensive downstream step.",
    ),
    SortStep: _StepHelp(
        title="Sort",
        what="Orders a table's rows by one column, ascending or descending.",
        tip="Dates are text (YYYYMMDD), so they sort correctly as plain text — no special "
        "date type needed.",
    ),
    DeriveStep: _StepHelp(
        title="Derive column",
        what=(
            "Adds a computed column from an existing one — extract the year/month from a "
            "date, split off the first party from a multi-party value, or change case."
        ),
        tip="Prefer deriving from transaction_date over recorded_date for time trends — "
        "recorded date lags the real event.",
    ),
    AggregateStep: _StepHelp(
        title="Aggregate",
        what=(
            "Groups rows by one or more columns and counts them — the engine behind every "
            "leaderboard (top buyers, patents per year, ...)."
        ),
        tip="Creates a brand-new table — it needs its own Export step, since it isn't the "
        "same table the aggregate ran on.",
    ),
    ExportStep: _StepHelp(
        title="Export",
        what=(
            "Writes one or more working tables to a file (Parquet / CSV / Excel / JSON / "
            "Feather), optionally choosing/reordering/renaming the final columns."
        ),
        tip="Usually the last step — nothing after an Export step can see its column "
        "choices, since they only affect the written file.",
    ),
}


def _column_effect(step: BatchStep) -> str:
    """The columns/tables this step adds, computed from the schema engine (``""`` if none).

    Reuses ``columns_after`` (the same engine ``validate_template`` runs on) rather than a
    hand-written per-kind description, so this text can never drift from real behavior.
    """
    load = LoadConfig()
    before = columns_after(load, [step], 0)
    after = columns_after(load, [step], 1)
    pieces: list[str] = []
    for table, cols in after.items():
        if table not in before:  # a table this step creates (aggregate / cpc_match output)
            shown = ", ".join(cols[:_MAX_LISTED_COLUMNS])
            extra = len(cols) - _MAX_LISTED_COLUMNS
            more = f", +{extra} more" if extra > 0 else ""
            pieces.append(f"new table '{table}' ({shown}{more})")
            continue
        added = [c for c in cols if c not in before[table]]
        if added:
            pieces.append(f"{table}.{'/'.join(added)}")
    return "; ".join(pieces)


def _row_effect(step: BatchStep) -> str:  # noqa: PLR0911 - one return per row-affecting kind
    """A short phrase for how a step changes the ROW SET, or ``""`` if it leaves rows unchanged.

    The column effect alone reports "no new columns" for a Filter/Dedupe/Compare-drop step, which
    reads as if the step does nothing — so name what it does to rows instead.
    """
    if isinstance(step, FilterStep) and step.clauses:
        return f"filters rows ({len(step.clauses)} clause(s), {step.combine.upper()})"
    if isinstance(step, DedupeStep):
        key = ", ".join(step.subset) if step.subset else "whole row"
        return f"removes duplicate rows (key: {key})"
    if isinstance(step, TransferTypeStep):
        return f"keeps only {step.assignor_type} → {step.assignee_type} rows"
    if isinstance(step, CompareStep) and step.action in ("drop_matches", "keep_matches"):
        verb = "drops" if step.action == "drop_matches" else "keeps"
        return f"{verb} rows where {step.left} matches {step.right}"
    if isinstance(step, ReferenceMatchStep) and step.action in ("keep_matched", "drop_matched"):
        return "keeps only matched rows" if step.action == "keep_matched" else "drops matched rows"
    if isinstance(step, SortStep):
        return f"reorders rows by {step.column}"
    return ""


def _effect(step: BatchStep) -> str:
    """An exact, computed description of what this step instance does: columns added and/or rows
    changed. The column side is derived from the live schema engine, so it can't drift."""
    if isinstance(step, SelectStep):
        chosen = ", ".join(step.columns) if step.columns else "(none chosen)"
        return f"keeps {len(step.columns)} column(s): {chosen}"
    if isinstance(step, ExportStep):
        tables = ", ".join(step.tables) if step.tables else "all tables"
        return f"writes {tables} as {step.fmt}"

    parts: list[str] = []
    columns = _column_effect(step)
    if columns:
        parts.append(columns)
    if isinstance(step, FilterStep) and step.columns:
        parts.append(f"projects to {len(step.columns)} column(s): {', '.join(step.columns)}")
    rows = _row_effect(step)
    if rows:
        parts.append(rows)
    return "; ".join(parts) if parts else "no new columns"


def step_help_html(step: BatchStep) -> str:
    """Full HTML for the Help panel when a step is selected in the steps list."""
    info = _STEP_HELP.get(type(step))
    if info is None:  # exhaustive by construction; keeps this honest if a new kind is added
        return "<p>No help available for this step kind.</p>"
    body = [
        f"<h3>{html.escape(info.title)} step</h3>",
        f"<p>{html.escape(info.what)}</p>",
        f"<p><b>On this step:</b> {html.escape(_effect(step))}</p>",
        f'<p style="color:#6D6D6D;">{html.escape(describe_step(step))}</p>',
    ]
    if info.tip:
        body.append(f"<p><b>Tip:</b> {html.escape(info.tip)}</p>")
    if not step.enabled:
        body.append(
            '<p style="color:#B00020;"><b>Disabled</b> — this step is skipped when the '
            "pipeline runs.</p>"
        )
    return "\n".join(body)


def step_note_text(step_type: type[BatchStep]) -> str:
    """Plain-text one-paragraph help for a step kind, for the step-editor dialogs.

    Same source as the Help panel (``_STEP_HELP``), so the note a user sees while *configuring* a
    step can never drift from the panel's explanation. Returns ``""`` for an unknown kind.
    """
    info = _STEP_HELP.get(step_type)
    if info is None:
        return ""
    return f"{info.what} {info.tip}".strip()


# --------------------------------------------------------------------------------------
# Template help — curated per shipped template name, with a generic fallback
# --------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _TemplateHelp:
    """Curated help text for one named template."""

    summary: str
    produces: str
    needs: str = ""


_TEMPLATE_HELP: dict[str, _TemplateHelp] = {
    # -- 01-14: the recommended numbered set (templates/0N_*.json) -------------------
    "01 - Firm-to-firm transactions (clean, enriched)": _TemplateHelp(
        summary=(
            "Row-level export of clean company-to-company patent transfers: the seller must "
            "resolve against the reference gazetteer, the buyer is classified and flagged "
            "against it too, canonical names and the buyer's gazetteer id are attached, and "
            "self-transfers (reorgs, subsidiary moves) are removed."
        ),
        produces="flat.parquet — one row per surviving transfer, with *_canonical / "
        "*_assignee_id columns and a transaction year.",
        needs="reference/reference.parquet (build once via Import… ▸ Build compact…).",
    ),
    "02 - Buyer leaderboard - deals closed": _TemplateHelp(
        summary=(
            "Ranks buyers by number of distinct assignment records (deals) they closed — "
            "deduped to one row per reel/frame before counting, so a multi-patent deal "
            "counts once."
        ),
        produces="buyers_by_deals.csv — buyer name, deal count.",
        needs="reference/reference.parquet.",
    ),
    "03 - Buyer leaderboard - patents (documents) acquired": _TemplateHelp(
        summary=(
            "Ranks buyers by distinct granted documents acquired — same gates as 02, but "
            "counts patents, not deals, so a multi-patent deal contributes one row per "
            "patent."
        ),
        produces="buyers_by_documents.csv — buyer name, document row count, distinct "
        "granted-document count.",
        needs="reference/reference.parquet.",
    ),
    "04 - Buyers - gazetteer-matched (entity-accurate leaderboard)": _TemplateHelp(
        summary=(
            "The strictest leaderboard — only buyers CONFIRMED in the gazetteer (matched by "
            "id, not just fuzzy name) are counted, grouped by their gazetteer id so name "
            "variants of the same legal entity never split across rows."
        ),
        produces="matched_buyers.csv — gazetteer assignee id + disambiguated name + count.",
        needs="reference/reference.parquet.",
    ),
    "05 - Buyers - off-gazetteer (NPEs / shells) for review": _TemplateHelp(
        summary=(
            "The mirror of 01 — exports firm-to-firm deals whose BUYER did NOT match the "
            "gazetteer (candidate NPEs, shells, or new entities), sorted by canonical buyer "
            "name, for manual review."
        ),
        produces="flat.csv — row-level, sorted by buyer, unmatched-buyer deals only.",
        needs="reference/reference.parquet.",
    ),
    "06 - Firm-to-firm buyers (rules only, no reference file)": _TemplateHelp(
        summary=(
            "The no-gazetteer alternative to 01 — classifies buyer and seller as companies "
            "using name rules only (no reference file needed), and still removes "
            "self-transfers."
        ),
        produces="flat.parquet — firm-to-firm transfers, rules-classified.",
        needs="nothing external — no reference file required.",
    ),
    "07 - CPC patent list per buyer (bridge for downstream CPC match)": _TemplateHelp(
        summary=(
            "Same gating as 01, kept row-level (not deduped or aggregated) specifically so a "
            "later Fetch CPC / CPC match step can read one row per patent per confirmed "
            "buyer — the bridge into CPC-based buyer discovery."
        ),
        produces="flat.csv — confirmed firm-to-firm transfers, one row per patent.",
        needs="reference/reference.parquet.",
    ),
    "08 - CPC enrich (firm-to-firm buyers + CPC codes)": _TemplateHelp(
        summary=(
            "Like 06 (rules-only firm-to-firm, no gazetteer), plus a Fetch CPC codes step "
            "that attaches each patent's CPC classification from the project's CPC data "
            "source."
        ),
        produces="flat.parquet — firm-to-firm transfers with cpc_codes / cpc_subclasses / "
        "cpc_lookup_status attached.",
        needs="the project CPC data source (Settings ▸ CPC data source) — offline by default.",
    ),
    "09 - CPC match to sales-package portfolio": _TemplateHelp(
        summary=(
            "Ranks buyers by CPC overlap against a portfolio of patents you're selling — "
            "resolves the portfolio's own CPC footprint from the CPC data source rather "
            "than a pre-built file. Run on the output of a template that already attached "
            "cpc_codes (e.g. 08)."
        ),
        produces="matched_buyers_by_portfolio_patent.csv + matched_buyers_overall.csv.",
        needs="portfolio.txt (one grant number per line) and cpc_codes already attached on "
        "the input.",
    ),
    "10 - Dropped sellers audit (off-gazetteer assignors)": _TemplateHelp(
        summary=(
            "The audit companion to 01 — same conveyance/housekeeping gate, but instead of "
            "keeping only gazetteer-matched sellers it flags the match and exports the ones "
            "that DIDN'T match, each with their closest gazetteer score, so you can see "
            "exactly what 01's gate excluded."
        ),
        produces="flat.csv — dropped seller name + best gazetteer score for every seller 01 "
        "would exclude.",
        needs="reference/reference.parquet.",
    ),
    "11 - Attach CPC from file (firm-to-firm, offline PatSeer join)": _TemplateHelp(
        summary=(
            "Firm-to-firm transfers (rules-classified, self-transfers removed) with CPC "
            "codes joined from a file you supply (e.g. a PatSeer export) — fully offline, "
            "no API call."
        ),
        produces="flat.csv — a fixed column set including cpc_codes / cpc_subclasses / "
        "cpc_lookup_status.",
        needs="cpc/patseer_export.csv (or your own CPC export file).",
    ),
    "12 - Convert to Parquet (all tables)": _TemplateHelp(
        summary=(
            "The simplest template — no filtering or enrichment, just writes every parsed "
            "table straight to Parquet. Useful as a fast raw-data conversion, or as an "
            "input for tools outside this app."
        ),
        produces="assignments / assignors / assignees / properties / flat.parquet — unmodified.",
        needs="nothing.",
    ),
    "13 - Attach CPC from file → Parquet (offline, ready for match)": _TemplateHelp(
        summary=(
            "Like 11, but keeps going: adds the firm-to-firm gate, canonical buyer names "
            "(match-only, so results stay reproducible), and CPC codes, writing Parquet "
            "ready for template 14's CPC match."
        ),
        produces="flat.parquet — firm-to-firm transfers with assignee_names_canonical + CPC "
        "columns attached.",
        needs="cpc/patseer_export.csv.",
    ),
    "14 - CPC match (offline footprint) + per-class matches": _TemplateHelp(
        summary=(
            "Ranks buyers by CPC overlap against a pre-built portfolio footprint file "
            "(patent, cpc pairs) — fully offline, no network, no API budget used. Run on "
            "13's output. Also emits the full per-class evidence table."
        ),
        produces="matched_buyers_by_portfolio_patent.csv, matched_buyers_overall.csv, "
        "matched_cpc_classes.csv.",
        needs="cpc/portfolio_footprint.csv (a patent, cpc footprint file) and cpc_codes already "
        "attached on the input (e.g. from 13).",
    ),
    # -- pre-review baseline (kept only for comparison) -------------------------------
    "Buyers - firm-to-firm transactions (clean, enriched)": _TemplateHelp(
        summary=(
            "Pre-review baseline version of template 01 (single-spelling conveyance filter, "
            "no id-based self-transfer gate) — kept for comparison. Prefer 01 or the "
            "[reviewed] version."
        ),
        produces="flat.parquet.",
        needs="reference/g_assignee_disambiguated.tsv.",
    ),
    "Buyer leaderboard - patents acquired": _TemplateHelp(
        summary="Pre-review baseline of template 03 — kept for comparison. Prefer 03 or "
        "the [reviewed] version.",
        produces="buyers_by_patents.csv.",
        needs="reference/g_assignee_disambiguated.tsv.",
    ),
    "Buyer leaderboard - deals closed": _TemplateHelp(
        summary="Pre-review baseline of template 02 — kept for comparison. Prefer 02 or the "
        "[reviewed] version.",
        produces="buyers_by_deals.csv.",
        needs="reference/g_assignee_disambiguated.tsv.",
    ),
    "Firm-to-firm buyers (rules only, no reference file)": _TemplateHelp(
        summary="Pre-review baseline of template 06 — kept for comparison. Prefer 06 or the "
        "[reviewed] version.",
        produces="flat.parquet.",
        needs="nothing external.",
    ),
    # -- hardened alternates (buyer_identification_templates.reviewed.json) ----------
    "Buyers - firm-to-firm (strict gate, enriched) [reviewed]": _TemplateHelp(
        summary=(
            "The strict-recall twin of 01 — both parties must clear the gazetteer/company "
            "gate, self-transfers are removed by id AND by a slightly looser fuzzy compare "
            "(≥92), and the export renames columns to buyer/seller_clean/id for direct "
            "spreadsheet use."
        ),
        produces="flat.parquet — renamed seller_clean/seller_id/buyer_clean/buyer_id/"
        "buyer_in_gazetteer columns.",
        needs="reference/reference.parquet.",
    ),
    "Buyers - firm-to-firm (recall gate: unmatched = unconfirmed) [reviewed]": _TemplateHelp(
        summary=(
            "A looser alternative to the strict gate — classifies both parties by rules "
            "first (instead of requiring a gazetteer hit) and only flags, rather than "
            "requires, a gazetteer match, so more deals survive at the cost of weaker "
            "buyer/seller confirmation."
        ),
        produces="flat.parquet — renamed seller_clean/seller_in_gazetteer/buyer_clean/"
        "buyer_in_gazetteer columns.",
        needs="reference/reference.parquet.",
    ),
    "Buyer leaderboard - distinct granted documents [reviewed]": _TemplateHelp(
        summary="Hardened twin of 03 — same distinct-document counting, with the id-based "
        "self-transfer gate template 04 uses.",
        produces="buyers_by_granted_docs.csv.",
        needs="reference/reference.parquet.",
    ),
    "Buyer leaderboard - deals closed [reviewed]": _TemplateHelp(
        summary="Hardened twin of 02 — dedupes to one row per deal before counting and "
        "renames the output to buyer/deals.",
        produces="buyers_by_deals.csv — buyer, deals.",
        needs="reference/reference.parquet.",
    ),
    "Firm-to-firm buyers (rules only, no reference file) [reviewed]": _TemplateHelp(
        summary=(
            "Hardened twin of 06 — self-transfer removal uses a fuzzy compare (≥92) "
            "instead of requiring an exact canonical match, catching near-identical name "
            "variants an exact compare would miss."
        ),
        produces="flat.parquet — renamed seller_clean/buyer_clean columns.",
        needs="nothing external.",
    ),
    # -- examples.json: generic teaching recipes --------------------------------------
    "Enrich flat (clean names + types)": _TemplateHelp(
        summary=(
            "The smallest useful recipe — normalizes and classifies both assignor and "
            "assignee names, with no filtering. A good starting point to build a custom "
            "pipeline from."
        ),
        produces="flat.parquet — *_canonical and *_type columns added.",
    ),
    "Firm-to-firm transfers, enriched": _TemplateHelp(
        summary=(
            "Filters to assignment-type conveyances in a date range, classifies both "
            "parties as company, keeps only firm-to-firm transfers, and exports a compact "
            "renamed column set."
        ),
        produces="flat.parquet — assignor_clean / assignee_clean / year.",
    ),
    "Individual-to-company transfers": _TemplateHelp(
        summary=(
            "The inverse of firm-to-firm — keeps only transfers from an individual "
            "(inventor/assignor) to a company buyer, e.g. a university or startup "
            "acquiring an inventor's patent."
        ),
        produces="flat.parquet.",
    ),
    "Remove self-transfers (assignor == assignee)": _TemplateHelp(
        summary="A minimal, reusable recipe — normalize both name columns, then drop rows "
        "where the canonical assignor and assignee are the same entity.",
        produces="flat.parquet, self-transfers removed.",
    ),
    "Top assignees by patent count": _TemplateHelp(
        summary=(
            "Ranks assignees by distinct granted patents received, across ALL conveyance "
            "types (not just genuine sales) — a quick “who's accumulating "
            "patents” view."
        ),
        produces="flat_by_assignee_names_canonical.csv, sorted by patent count descending.",
    ),
    "Assignments per year": _TemplateHelp(
        summary=(
            "Counts distinct assignment records (deals) per year — dedupes to one row per "
            "reel/frame first so a multi-patent deal doesn't inflate the count."
        ),
        produces="flat_by_transaction_date_year.csv, sorted by year.",
    ),
    "Reference-match assignors to known companies": _TemplateHelp(
        summary="The simplest reference-match recipe — keeps only rows whose assignor "
        "matches a known company in a disambiguated-assignee reference file.",
        produces="flat.parquet, unmatched-assignor rows dropped.",
        needs="reference/g_assignee_disambiguated.tsv (or your own reference file).",
    ),
    # -- built-in presets ("New from example ▾" menu; not files) ---------------------
    "Firm-to-firm transfers": _TemplateHelp(
        summary="The one-click preset behind “New from example” — a bare "
        "Transfer type (company → company) step plus an export; a minimal starting "
        "point to extend.",
        produces="flat.parquet.",
    ),
    "Top assignees": _TemplateHelp(
        summary=(
            "The preset behind “New from example” — normalizes assignee names on "
            "the assignees table and counts occurrences per canonical name. Counts "
            "assignment mentions, not distinct patents — for a patent count, use the "
            "“Top assignees by patent count” example instead."
        ),
        produces="assignees_by_name_canonical.csv.",
    ),
    "Enrich flat (names + types)": _TemplateHelp(
        summary="The preset behind “New from example” — normalizes and classifies "
        "both party columns on flat, with no filtering.",
        produces="flat.parquet.",
    ),
}


def _pipeline_listing(steps: Sequence[BatchStep]) -> str:
    """A numbered ``<ol>`` of every step's one-line summary (matches the steps list order)."""
    items = []
    for step in steps:
        marker = " — disabled" if not step.enabled else ""
        items.append(f"<li>{html.escape(describe_step(step))}{html.escape(marker)}</li>")
    return "<ol>" + "".join(items) + "</ol>"


def template_help_html(name: str, steps: Sequence[BatchStep], description: str = "") -> str:
    """Full HTML for the Help panel when a saved template is selected (no step in focus).

    ``description`` is the template's own embedded help (from its JSON, if any). Precedence:
    a non-blank embedded description wins (so imported/user templates document themselves), else
    the curated built-in help for a shipped template, else a generic pipeline listing.
    """
    title = name.strip() or "(untitled template)"
    info = _TEMPLATE_HELP.get(name.strip())
    body = [f"<h3>{html.escape(title)}</h3>"]
    if description.strip():
        body.append(f"<p>{html.escape(description.strip())}</p>")
    elif info is not None:
        body.append(f"<p>{html.escape(info.summary)}</p>")
        body.append(f"<p><b>Produces:</b> {html.escape(info.produces)}</p>")
        if info.needs:
            body.append(f"<p><b>Needs:</b> {html.escape(info.needs)}</p>")
    else:
        body.append(
            "<p><i>No built-in description for this template</i> — here's its pipeline, "
            "step by step (click a step in the list for details on that step):</p>"
        )
    if steps:
        body.append(f"<p><b>Pipeline</b> ({len(steps)} step(s)):</p>")
        body.append(_pipeline_listing(steps))
    else:
        body.append('<p><i>No steps yet — use "Add step" to build the pipeline.</i></p>')
    return "\n".join(body)


def welcome_help_html() -> str:
    """Full HTML for the Help panel when nothing is selected."""
    return (
        "<h3>Help</h3>"
        "<p>Select a saved template above, or click a step in the steps list, to see a "
        "crisp explanation of what it does here.</p>"
        "<p>A template is an ordered pipeline: each step transforms the working tables "
        "(assignments / assignors / assignees / properties / flat) in place, top to "
        "bottom, before the last step(s) export the result.</p>"
    )
