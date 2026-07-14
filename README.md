# assignment_uspto

Type: **script**. Purpose: parse USPTO **patent-assignment XML** into analysis-ready
**Parquet** and **Excel** tables.

> 🏢 **Buyer-identification pipeline** — `uspto-assign ingest / build-dictionary / ledger /
> report`: entity resolution (exact → fuzzy → stable provisional ids) over a build-once local
> dictionary, a firm-to-firm **transaction ledger**, honest buyer leaderboards, and a
> **CPC-ready** buyer×patent bridge. See `GUIDE.md` §3b.
>
> 📖 **See [`GUIDE.md`](GUIDE.md) for the full user manual** — CLI, desktop app, filters,
> normalization, classification, reference matching, and the complete batch-processing step catalog
> with end-to-end recipes.

Given a USPTO assignment XML file (a single search-result export or a large bulk daily/annual
dump), it streams the file with bounded memory and extracts every field into four normalized
tables plus a wide flat view:

| Table | Grain | Key |
|---|---|---|
| `assignments` | one row per assignment record (reel/frame header, dates, conveyance, correspondent) | `reel_no` + `frame_no` |
| `assignors` | one row per assignor (name, execution date) | → assignment |
| `assignees` | one row per assignee (name, address, city/state/country/postcode) | → assignment |
| `properties` | one row per patent-property × document-id (country, doc number, kind, date, title) | → assignment |
| `flat` | denormalized: one row per property, with assignor/assignee names concatenated | — |

Each table is written to its own **Parquet** file (the complete source of truth — all values
kept as text so significant leading zeros in reel/frame/doc numbers survive) and to a single
multi-sheet **Excel** workbook (one sheet per table). Both writers stream — Parquet flushes in
batches, Excel uses openpyxl `write_only` mode — so peak memory stays flat even on multi-GB bulk
files. Any sheet exceeding Excel's ~1,048,576-row cap is truncated (with a warning); the Parquet
file for that table is always complete.

## Setup

This project uses **venv + pip** (uv was not installed at scaffold time).

```bash
python3 -m venv .venv
.venv/bin/pip install lxml pyarrow openpyxl                 # runtime deps
.venv/bin/pip install pytest ruff pyright lxml-stubs pyarrow-stubs   # dev + type stubs
```

### Windows

The commands are identical apart from the venv path: Windows puts executables in
`.venv\Scripts\` (not `.venv/bin/`). Install **Python 3.12+** from
[python.org](https://www.python.org/downloads/) with *"Add python.exe to PATH"* ticked, then:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1        # PowerShell  (cmd.exe: .venv\Scripts\activate.bat)
pip install -e ".[dev,ui]"         # core + dev tools + desktop UI (PyQt6)
```

Once the venv is **activated** (your prompt shows `(.venv)`), plain `python` / `pip` / `pytest`
/ `uspto-assign` all use it, so you can drop the `.venv\Scripts\` prefix from every command
below. If PowerShell refuses to run the activate script, run once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. To run without activating, prefix the
full path instead, e.g. `.venv\Scripts\python.exe main.py parse assignment.xml --outdir out`.
Use backslashes in Windows paths (`out\assignments.xlsx`).

## Run

The input may be a raw `.xml` **or** the `.zip` exactly as downloaded from USPTO — the XML is
read straight from the archive, so there's no need to unzip a multi-GB file first.

```bash
# Both formats into ./out/ (default) — zip input works directly
.venv/bin/python main.py path/to/ad20260709.zip

# Choose output dir, formats, and Excel workbook name
.venv/bin/python main.py assignment.xml --outdir out --formats parquet,excel --basename assignments

# Parquet only (skips the Excel step — best for very large bulk files)
.venv/bin/python main.py ad20260709.zip --formats parquet -v
```

Outputs: `out/assignments.parquet`, `out/assignors.parquet`, `out/assignees.parquet`,
`out/properties.parquet`, and `out/assignments.xlsx` (with a `flat` sheet) — plus a
`manifest.json` audit record (command, input, duration, every output file with row counts).
`uspto-assign templates-summary` regenerates `templates/TEMPLATES.md`, the reviewable numbered
steps summary + validation warnings for every bundled template.

## Desktop UI (PyQt6, Metro style)

A native desktop viewer explores the parsed data interactively:

- **Blank start** with a tiles landing (Open XML/ZIP, Open dataset folder, View Parquet/data file,
  Recent) and a persistent **toolbar** (Open · Save processed · Export · Manage queries · Close).
- **Parquet / data-file viewer** — *View Parquet / data file…* opens any single
  **`.parquet`/`.arrow`/`.feather`/`.csv`** as a one-table view (values shown as text, list columns
  joined). Then filter/sort/paginate, **Edit columns…** (keep / reorder / rename / drop), and
  **Export** to convert it to CSV/JSON/Excel/Parquet/Feather — a standalone viewer/editor/converter
  for arbitrary tabular files (e.g. a batch run's per-step trace outputs).
- **Initial-load template** — pick which fields/tables to load, cap the record count, choose a page
  size.
- **Filter/query builder** — per-field clauses with **categorical value dropdowns** (low-cardinality
  columns like `conveyance_text`/`doc_kind` pre-fill their distinct values), **smart operator
  defaults** (dates → *In range*, categorical → *Equals*, else *Contains*), an **AND/OR** combine
  toggle, quick search, click-to-sort, and **pagination**.
- **Saved queries** — save the current filter/sort by name and re-apply it later (persisted to the
  OS config dir); manage/apply/delete via **Queries ▸ Manage**.
- **Save processed dataset** as **Parquet** (portable) or **Arrow/Feather** (fastest reopen) and
  reopen either directly — *Open dataset folder* auto-detects the format.
- **Multi-format export** (Parquet/Excel/CSV/JSON/Feather) scoped to **all / filtered view /
  selected rows**, plus one-click "export all tables". Files are named after the **source** (with
  a `_filtered`/`_selected` suffix for subsets) and never overwrite — a ` (n)` counter is added.
- **Batch processing** (**Settings ▸ Batch processing**) — a Power-Query-style **Applied-Steps**
  pipeline builder: reusable **templates** of atomic steps (**filter**, **normalize**, **classify**,
  **compare**, **transfer type**, **reference match**, **fetch CPC**, **attach CPC from file**,
  **CPC match**, **deduplicate**, **select**, **sort**, **derive**, **aggregate**, **export**) that
  you **reorder / duplicate / enable-disable / insert**,
  **double-click to edit**, and validate (⚠ badges for missing columns). Dialogs are **schema-aware**
  (they offer columns added by earlier steps); the **Export** step can pick, **order, and rename** the
  final columns; a **Preview** runs the pipeline on a ~1,000-row sample and shows each step's result +
  row/column deltas; templates **duplicate / import / export** and ship with **example presets**.
  Point them at many `.xml`/`.zip` files or dataset folders and run in the background with a
  **colour-coded live console**, a **determinate progress bar**, a **Cancel** button (per-file
  granularity; closing the window mid-run prompts and closes safely once the run stops), and
  per-file error isolation. **Every run is a self-contained, audit-ready folder**
  (`<out>/<template>/run_<timestamp>/`): `manifest.json` (template + step summaries, validation
  warnings, per-file **per-step row deltas**, output paths), `summary.xlsx` (the same as
  run/steps/outputs sheets), `run.log`, and the per-source outputs — plus a rolling
  `runs_index.csv` at the output root (one line per run, across all templates). Templates are
  **validated before every run** (warnings continue by default; `run_batch(strict=True)` aborts).
  The output folder defaults to your last-used dir (or `data/out`) and file dialogs remember where
  you last picked. **Save each step's output** (a per-run checkbox) additionally writes every
  enabled step's resulting table to `<source>/steps/NN_<table>.parquet` — one lossless, reopenable
  file per step — so you can open and validate each intermediate (best on a shortlisted set).
  Sequential by default; *Workers > 1* processes files in parallel (the console
  shows distinct worker PIDs + interleaved per-file progress + a combined total). The parser
  **skips building unused tables** (notably the wide `flat` table) for speed.
  - **Analysis steps**: *Deduplicate* (keep-first, by chosen key columns), *Select columns*,
    *Sort*, *Derive column* (year/month of a date, first split part, upper/lower case), and
    *Aggregate* — group by columns and **count** rows (plus an optional distinct count) into a new
    summary table, exportable (e.g. patents per canonical assignee, assignments per recorded year).
  - **Relationship steps**: *Classify entity type* (label a name column **company / individual /
    unknown**), *Compare columns* (match assignor vs assignee — flag, or **drop / keep** matching
    rows, e.g. remove self-transfers), and *Transfer type* — a one-click preset that keeps only a
    chosen pairing (**firm→firm**, individual→firm, …). Classification is rule-based by default
    (legal-suffix/org-keyword detection + `LAST, FIRST` person patterns); installing the optional
    `ml` extra (`pip install -e ".[ml]"`) enables a `probablepeople` ML backend, selectable per step.
  - **Match against reference** — *Match against reference* fuzzy-matches a name column against an
    external **USPTO/PatentsView disambiguated-assignee** file (a company gazetteer). A match
    normalizes the raw name to the disambiguated organization and captures its `assignee_id`
    (adding `<col>_disambiguated` / `<col>_matched` / `<col>_assignee_id`, and optionally
    `<col>_match_score` / `<col>_match_review` — see match confidence below); *keep-matched* drops
    the rest (presumed individuals). Validation checks the reference file exists **and actually
    has** the configured name/id columns before the run. Point it at the raw `g_assignee_disambiguated.tsv` (configurable
    name/id column + delimiter, streamed) or pre-extract distinct organizations into a small
    reusable Parquet — either `uspto-assign build-reference <tsv>` (columns auto-detected) or the
    **Build compact…** button. Blocked fuzzy matching keeps it fast (millions of rows in seconds).
  - **CPC enrichment & portfolio matching** — *Fetch CPC* attaches classification codes to a
    patent-number column (`cpc_codes` / `cpc_subclasses` / `cpc_lookup_status`) via the live
    **USPTO Open Data Portal API** (`api.uspto.gov`, `X-API-KEY`; configure it in
    **Settings ▸ CPC / USPTO API data source**, which has a **Test connection** button and is
    **offline by default** — fetch needs an exported `USPTO_ODP_API_KEY`, the network posture set
    to allow, and a per-run *Allow network* tick). *Attach CPC from file* does the same join
    **fully offline from an uploaded PatSeer/CSV/Parquet export** (a separator splits multi-code
    cells) — no key, no network. *CPC match* then ranks buyers per sales-package patent by CPC
    overlap. Bundled templates `08`–`11` cover the enrich → match → file-attach flows.
- **Name normalization** — a normalize step fuzzy-matches (rapidfuzz) an assignor/assignee name
  column to canonical forms, adding a `<column>_canonical` column (auto-derived, so multiple
  normalize steps never clobber each other). Matching is **exact-alias-first then fuzzy**, and
  fuzzy candidates are **blocked by name prefix** so cost stays low as the memory grows. A
  *split separator* (auto-suggested `"; "` for concatenated `*_names` columns) normalizes each
  part; a *match-only* toggle uses a curated memory without adding new canonicals; and a *scorer*
  choice selects the rapidfuzz algorithm (WRatio, token-set, token-sort, partial, Jaro-Winkler…).
  **Match confidence & review**: the fuzzy steps (normalize / reference match / compare) can emit a
  `*_score` column (0–100, weakest party) and a `*_review` flag for matches accepted below a chosen
  bar — the clerical-review band (typical setup: threshold 90, review below 95).
  The **learnable entity memory** (**Settings ▸ Entity memory**) is deduplicated, seeds from a
  CSV/JSON file **or a multi-GB disambiguated-assignee reference** (*Seed from reference…*,
  streamed off the GUI thread), and is stored in a **relocatable project file** (default
  `entities.json`; fuzzy-learned aliases carry their learn-time score — v1 files load unchanged).
  Its dialog is a **full editor** — searchable **Canonicals** (add / rename / merge / delete) and
  **Aliases** tabs with a **Score column** and a **review queue** (*Only aliases learned below N* →
  *Mark reviewed* / reassign / delete), with *Save*/*Cancel* (edits rebuild the fuzzy block index);
  plus Import / Export / *Change location…* / *Clear*. The memory file is only rewritten when a run
  actually learned something.

Parsing runs on a background thread with a progress indicator, so multi-GB files never freeze the
window, and data is held in a **memory-mapped Arrow store** for low, flat RAM use. Subtle 1px
borders frame the table and filter panel; styling lives entirely in
`src/uspto_assignments_ui/resources/metro.qss`.

```bash
.venv/bin/pip install -e ".[ui,dev]"          # install the UI extra (PyQt6) + dev tools
.venv/bin/python run_ui.py path/to/ad20260709.zip   # open a USPTO file (or a dataset folder)
# or, once installed:  uspto-assign-ui
```

Then use **File ▸ Open** to load a `.xml`/`.zip`, add filter clauses, type in the search box, or
click a column header to sort. The styling lives entirely in
`src/uspto_assignments_ui/resources/metro.qss`.

## Package layout

- `src/uspto_assignments/` — Qt-free core: `model`, `parser`, `tables` (record→table + the
  memory-mapped Arrow-IPC store), `filters` (vectorized `pyarrow.compute`), `exporters`, `cli`.
- `src/uspto_assignments_ui/` — the PyQt6 app: `models/arrow_table_model`, `widgets/*`
  (`FilterBar`, `TablePanel`, `DataTable`, `PageTitle`/`SectionLabel`), `workers`, `main_window`,
  `app`. Imports the core, never the reverse.
- `main.py` / `run_ui.py` — thin launchers. See `docs/UI_PLAN.md` for the full design.

## Develop

```bash
.venv/bin/ruff format . && .venv/bin/ruff check --fix .   # format + lint
.venv/bin/pyright                                         # type-check (strict)
.venv/bin/pytest -q                                       # test
```

## Notes

- Parsing is **schema-tolerant**: missing or slightly renamed tags yield empty cells rather than
  crashing, so minor USPTO DTD-version differences are handled. If a field you expect comes back
  empty, check the tag names in your file against the extractors in
  `src/uspto_assignments/parser.py` — they are the one place to adjust.
- Dates and all identifiers are preserved verbatim as strings (USPTO dates can be partial, e.g.
  `20240000`, and identifiers carry leading zeros).
