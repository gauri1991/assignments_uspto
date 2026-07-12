# USPTO Patent-Assignment Tool — User Guide

The complete, living reference for everything this tool does: parsing USPTO patent-assignment data,
exploring it in the desktop app, and building **batch pipelines** that filter, normalize, classify,
match, and export it.

> This is the authoritative user manual. When features change, **keep this file in sync** — especially
> the [Batch step catalog](#batch-step-catalog) and [Recipes](#recipes). See
> [Maintaining this guide](#maintaining-this-guide).

## Contents

1. [Overview & data model](#1-overview--data-model)
2. [Install & run](#2-install--run)
3. [Command line (CLI)](#3-command-line-cli)
4. [Export formats](#4-export-formats)
5. [Desktop app tour](#5-desktop-app-tour)
6. [Filters](#6-filters)
7. [Entity normalization & the memory editor](#7-entity-normalization--the-memory-editor)
8. [Entity-type classification](#8-entity-type-classification-company-vs-individual)
9. [Reference matching (PatentsView disambiguated assignees)](#9-reference-matching-patentsview-disambiguated-assignees)
10. [Batch processing](#10-batch-processing)
    - [Batch step catalog](#batch-step-catalog)
    - [Recipes](#recipes)
11. [Config & file locations](#11-config--file-locations)
12. [Develop & quality gate](#12-develop--quality-gate)
13. [Maintaining this guide](#maintaining-this-guide)

---

## 1. Overview & data model

Given a USPTO patent-assignment **XML** file (a single search export or a large bulk daily/annual
dump) — or the **`.zip`** exactly as downloaded — the tool streams it with bounded memory and extracts
every field into five tables:

| Table | Grain (one row per…) | Key | Notable columns |
|---|---|---|---|
| `assignments` | assignment record (reel/frame header) | `reel_no` + `frame_no` | `recorded_date`, `conveyance_text`, `correspondent_*` |
| `assignors` | assignor (the party transferring) | → assignment | `name`, `execution_date`, `date_acknowledged` |
| `assignees` | assignee (the party receiving) | → assignment | `name`, `address_1/2`, `city`, `state`, `country_name`, `postcode` |
| `properties` | patent property × document-id | → assignment | `invention_title`, `doc_country`, `doc_number`, `doc_kind`, `doc_date` |
| `flat` | **denormalized**: one row per property | — | everything above + `assignor_names`, `assignee_names` (joined with `"; "`), `assignor_count`, `assignee_count`, `execution_date`, `date_acknowledged` (latest signer), `transaction_date` + `date_source` (execution date, else recorded) |

Key facts:

- **Every value is a string, preserved verbatim.** Leading zeros in reel/frame/doc numbers and partial
  dates (e.g. `20240000`) survive. Dates are `YYYYMMDD` text, so they sort/range lexicographically.
- **`flat`** is the analysis-ready single table: one row per patent property, with all assignor and
  assignee names concatenated (`"; "`-separated) into `assignor_names` / `assignee_names`.
- **Schema-tolerant parsing**: missing or slightly renamed tags yield empty cells instead of crashing,
  so minor USPTO DTD-version differences are handled. If a field comes back empty unexpectedly, check
  the tag names in your file against the extractors in `src/uspto_assignments/parser.py`.

The output is written as one **Parquet** file per table (the complete source of truth) and/or a single
multi-sheet **Excel** workbook (one sheet per table, plus a `flat` sheet). Both writers stream, so
peak memory stays flat even on multi-GB inputs. Any Excel sheet beyond ~1,048,576 rows is truncated
(with a warning); the Parquet for that table is always complete.

---

## 2. Install & run

Python **3.12+**. This project uses **venv + pip** (not uv).

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[ui,dev]"      # editable install with the UI + dev tools
```

Optional dependency extras:

| Extra | Adds | Needed for |
|---|---|---|
| `ui` | `PyQt6` | the desktop app |
| `ml` | `probablepeople` | the optional ML entity-type classifier (rules work without it) |
| `dev` | `pytest`, `pytest-qt`, `ruff`, `pyright`, `lxml-stubs`, `pyarrow-stubs` | development |

Entry points (installed as console scripts, or run the shims directly):

| Command | Shim | Purpose |
|---|---|---|
| `uspto-assign` | `python main.py` | CLI: parse → Parquet/Excel |
| `uspto-assign-ui` | `python run_ui.py` | launch the desktop app |

---

## 3. Command line (CLI)

The CLI now has **subcommands**; a bare input path still works as the legacy `parse`:

```bash
.venv/bin/uspto-assign parse INPUT [--outdir DIR] [--formats LIST] [--basename STEM] [--batch-size N]
.venv/bin/python main.py INPUT …                    # legacy invocation, identical behaviour
```

### 3b. Buyer-identification pipeline (entity resolution + transaction ledger)

A standalone pipeline (never touches the network at run time) that turns raw assignment data into a
**transaction ledger** with resolved buyer/seller **entities**, honest buyer leaderboards, and a
**CPC-ready patent bridge**:

```bash
# 1. Land the four raw tables as Parquet at natural grain (parse once)
.venv/bin/uspto-assign ingest ad20260709.zip --out data/raw            # [--limit N] for samples

# 2. Build the resolution dictionary ONCE from local seed files (PatentsView v1 seed;
#    add GLEIF/SEC/Wikidata extracts via the generic --extra PATH:NAME_COL:ID_COL:SOURCE)
.venv/bin/uspto-assign build-dictionary --patentsview reference/g_assignee_disambiguated.tsv \
    --out dictionary

# 3. Build the ledger (resolve mentions + reconstruct transactions + emit the three contracts)
.venv/bin/uspto-assign ledger --raw data/raw --dict dictionary --out data/ledger \
    [--include-types assignment,nunc_pro_tunc] [--scorer token_sort] [--threshold 92]

# 4. Leaderboards + optional CPC reconciliation (sub-second queries over the materialized ledger)
.venv/bin/uspto-assign report --ledger data/ledger --by patents --top 20 \
    [--cpc-mode sampled --sample 25] [--cpc-file g_cpc_current.tsv --cpc-patent-column patent_id]
```

How it works, in order: a curated **conveyance taxonomy** (regex → `assignment / security_interest /
release / merger / name_change / license / correction / nunc_pro_tunc / other`) cuts rows early by
*type*; **transactions** are reconstructed per `reel_no`+`frame_no` with `transaction_date` = the
latest assignor `execution_date` (fallback `recorded_date`; `date_source` records which); every
distinct party mention goes through the **resolution cascade** — O(1) exact alias lookup (incl. a
legal-suffix-stripped key) → person detector → **capped blocked fuzzy** (oversized prefix blocks are
re-split, so no pathological prefix degrades to a full scan) → **stable provisional ids** for
off-gazetteer entities (clustered so spelling variants collapse to one buyer, persisted so ids
survive across runs); the **firm-to-firm predicate runs on entities, not strings** (every seller and
buyer must resolve to an org, and seller/buyer ultimate-parent sets must be disjoint — inventors and
intra-group reorgs drop out cleanly).

The fuzzy layer defaults to **`token_sort` @ threshold 92** — order-insensitive but *not*
token-set (which over-merges distinct firms that share dominant generic tokens, e.g. collapsing many
different `SHENZHEN … TECHNOLOGY CO LTD` into one). Pass `--scorer token_set` (looser, parent/family
grain) or a lower `--threshold` when you deliberately want more aggressive merging.

Outputs in `--out` (Parquet, joined on `entity_id`): **`transaction_ledger`** (one row per
reel/frame: conveyance type, true date + source, seller/buyer entity ids + canonical names + parent
ids, property count), **`buyers`** (one row per resolved buyer: `resolution_source`, confidence,
`is_off_gazetteer`, deals/patents counts, first/last acquisition dates), and
**`buyer_property_bridge`** (one row per buyer × canonical property: `doc_type`
application/publication/grant, `patent_id_normalized` in PatentsView convention by default, and
`cpc_lookup_status`). `report --cpc-file <cpc table>` joins CPC and **attaches the codes to the
bridge**: `cpc_codes` (the distinct full CPC symbols for that patent — the column your downstream
portfolio-vs-buyer matching consumes) and `cpc_subclasses` (the distinct 4-char subclasses, `H04L …`,
for coarse-grain domain matching); non-grant rows carry empty lists. It also computes
**`cpc_hit_rate`** and warns loudly when the id formats are misaligned (`--cpc-code-column` names the
CPC-symbol column, default `cpc_group` for PatentsView `g_cpc_current`). `--cpc-mode sampled` caps
each buyer to its N most recent grants for a cheap domain-classification pass.

Time-axis note: `flat` carries `transaction_date` (the latest signer `execution_date`, or
`recorded_date` when none was filed) with a `date_source` flag, so trend analysis keys off the true
transfer date rather than the lagging record date.

Counting is honest by construction: **deals** = distinct reel/frame; **patents** = distinct
*canonical properties* (application/publication/grant rows of one invention collapse to one key), so
`doc_number` app/pub/grant double-counting cannot inflate leaderboards.

| Argument | Default | Meaning |
|---|---|---|
| `input` (positional) | — (required) | Path to the USPTO assignment `.xml` **or** `.zip` (the XML is read straight from the archive — no need to unzip). |
| `--outdir` | `out` | Output directory (created if missing). |
| `--formats` | `parquet,excel` | Comma-separated; valid values are `parquet` and `excel`. |
| `--basename` | `assignments` | Excel workbook filename stem (ignored for Parquet, which is one file per table). |
| `--batch-size` | `5000` | Records buffered before each Parquet flush. |
| `-v`, `--verbose` | off | Enable debug logging. |

Examples:

```bash
# Both formats into ./out/ — zip input works directly
.venv/bin/python main.py path/to/ad20260709.zip

# Parquet only (skips Excel — best for very large bulk files)
.venv/bin/python main.py ad20260709.zip --formats parquet -v

# Choose output dir + Excel workbook name
.venv/bin/python main.py assignment.xml --outdir out --formats parquet,excel --basename mydata
```

Outputs: `out/assignments.parquet`, `out/assignors.parquet`, `out/assignees.parquet`,
`out/properties.parquet`, and `out/assignments.xlsx` (with a `flat` sheet).

---

## 4. Export formats

The desktop app and batch **Export** step write these formats:

| Format | Extension | Notes |
|---|---|---|
| Parquet | `.parquet` | Columnar, portable — the recommended default. |
| CSV | `.csv` | Plain comma-delimited. |
| Excel | `.xlsx` | Streamed (openpyxl); slowest for large tables; sheet capped at ~1,048,576 rows. |
| JSON | `.json` | Array of row objects. |
| Feather (Arrow) | `.arrow` | Arrow IPC — fastest to re-open. |

Export **scope** in the app: **All rows**, **Filtered view** (the current filter result), or
**Selected rows** (the rows you highlighted). Files are named after the source; subsets get a
`_filtered` / `_selected` suffix and never overwrite — a ` (n)` counter is appended.

> **Tip:** For tables over ~100k rows prefer **Parquet** or **CSV** over Excel — Excel is by far the
> slowest writer.

---

## 5. Desktop app tour

Launch with a file, a dataset folder, or blank:

```bash
.venv/bin/python run_ui.py path/to/ad20260709.zip   # open a file or dataset folder
.venv/bin/python run_ui.py                           # blank start (landing page)
```

**Landing page** — tiles to *Open XML / ZIP* or *Open dataset folder*, plus a **Recent** grid
(hidden until you've opened something).

**Menus & toolbar** (exact labels):

- **File**: `Open XML/ZIP…` (Ctrl+O) · `Open dataset folder…` · `Save processed…` (Ctrl+S) ·
  `Export current table…` (Ctrl+E) · `Export all tables…` · `Close dataset` (Ctrl+W) · `Exit`.
- **Queries**: `Save current query…` · `Manage queries…`.
- **Settings**: `Batch processing…` (Ctrl+B) · `Entity memory…`.
- **Toolbar**: Open · Open dataset · Save · Export current · Export all · Manage queries · Batch · Close.

**Load options** (shown when opening) — pick which tables/fields to load (a checkable
table→columns tree), cap the record count, and choose a page size (100 / 500 / 1,000 / 5,000 / All;
default 1,000).

**Data view** — one tab per table (label shows the row count). Each tab has:

- A **filter/query builder** (see [Filters](#6-filters)): a quick **search box** (250 ms debounce,
  searches all columns), a clause builder (column · operator · value(s) · *Add filter*), an
  **AND/OR** combine toggle (*Match all* / *Match any*), removable filter **chips**, and *Clear all*.
- **Click a column header** to sort (toggles ascending/descending).
- A **pager** at the bottom (First / Prev / Next / Last + range label).

**Saved queries** — *Queries ▸ Save current query…* stores the current filter+sort by name;
*Manage queries…* lists them (`name · table`) to Apply or Delete. Persisted to the OS config dir.

**Save processed** — *File ▸ Save processed…* writes the loaded dataset as **Parquet** (portable) or
**Arrow/Feather** (fastest reopen). Re-open either via *Open dataset folder* (the format is
auto-detected).

**Export** — *Export current table…* (with a format + scope dialog) or *Export all tables…*.

---

## 6. Filters

A filter is one or more **clauses** combined with **AND** (match all) or **OR** (match any). A
`FilterClause` has: `column`, `op`, `value` (also the lower bound for ranges), `value2` (upper
bound), and `case_sensitive`.

| Operator | Needs value? | Meaning |
|---|---|---|
| `contains` | yes | substring match |
| `equals` | yes | exact match (case-insensitive unless `case_sensitive`) |
| `not_equals` | yes | null-safe exclusion — keeps rows whose value differs (nulls kept) |
| `starts_with` | yes | prefix match |
| `in_range` | value + value2 | `value ≤ x ≤ value2`, **lexicographic** (dates as `YYYYMMDD` order correctly) |
| `not_empty` | no | value present and non-empty |
| `is_empty` | no | null or empty |

In the app, the operator auto-defaults smartly: `*_date` columns → *In range*, low-cardinality
(categorical) columns → *Equals*, everything else → *Contains*. Categorical columns pre-fill their
distinct values in the value dropdown.

Everything is string-based (all columns are text), so comparisons are textual. **Filters compare a
column against a literal — they can't compare two columns to each other.** To compare two columns
(e.g. assignor vs assignee), use the [Compare](#compare) or
[Match against reference](#match-against-reference) batch step to write a flag column, then filter on
that flag.

---

## 7. Entity normalization & the memory editor

Real assignor/assignee names are messy (`ADOBE SYSTEMS INC`, `Adobe Systems, Inc.`, `ADOBE SYSTEMS
INCORPORATED`). **Normalization** collapses variants to one canonical form.

**How matching works** (fast, in this order):

1. **Clean** the name — uppercase, strip punctuation, collapse whitespace.
2. **Exact alias** — an O(1) lookup of names already resolved.
3. **Blocked fuzzy** — [rapidfuzz](https://github.com/rapidfuzz/RapidFuzz) compares only against
   canonicals **sharing the same 4-character prefix** (blocking), so cost stays low even with 100k+
   canonicals. Any single prefix that accumulates more than ~10,000 names (e.g. the ~7k gazetteer
   orgs starting `THE …`) is automatically **re-split onto a longer prefix (6–8 chars)** so one
   crowded prefix can never degrade a match to a full scan. This cap is shared by name
   normalization, the **Match against reference** step, and the ledger pipeline — run with `-v` to
   log each gazetteer's largest fuzzy block.

The **scorer** (fuzzy algorithm) is selectable:

| Scorer | Good for |
|---|---|
| `wratio` (default) | general robustness to length/word differences |
| `token_set` | reordered words + extra tokens (great for company names) |
| `token_sort` | word-order differences |
| `partial` | one name is a substring of the other |
| `qratio` / `ratio` | strict character similarity |
| `jaro_winkler` | short strings, shared prefixes |

The **match threshold** (0–100, default **90**) is the minimum score to count as a match.

### The learnable entity memory

Normalization is backed by an **entity memory**: a deduplicated set of canonical names plus learned
`alias → canonical` mappings. It **grows as it resolves names** (unless a step sets *match-only*), and
persists to JSON. It's stored in a **relocatable project file** (default `entities.json` in the
working folder); a pointer file remembers the chosen location across sessions.

### The memory editor — *Settings ▸ Entity memory*

A tabbed editor that works on a **working copy** (Save persists, Cancel discards):

- **Canonicals** tab — searchable list; **Add**, **Rename** (repoints its aliases), **Merge** (fold
  one canonical into another), **Delete** (also removes its aliases). Structural edits rebuild the
  fuzzy block index so matching keeps working.
- **Aliases** tab — searchable table of `alias → canonical`; **double-click a canonical cell to
  reassign** an alias, or **Delete alias**.
- File actions — **Import…** (seed from CSV `alias,canonical` / JSON / one-name-per-line), **Export…**
  (save the memory as JSON), **Change location…** (relocate the active file), **Clear** (reset to
  empty — use this to discard junk accumulated from a bad run).

> **Match-only vs learning:** a Normalize step with *Learn new canonicals* **off** matches against the
> memory without adding anything — use it with a curated memory so processing doesn't pollute it.

---

## 8. Entity-type classification (company vs individual)

Classification labels a name **`company`**, **`individual`**, or **`unknown`** — used to isolate
firm-to-firm transfers and to identify individual assignors (inventors).

**Rule-based** (default, no dependency, fast):

- **Company** — the name contains a legal-form or organization keyword. Legal forms include `INC,
  INCORPORATED, CORP, CORPORATION, CO, COMPANY, LLC, LLP, LP, LTD, LIMITED, PLC, GMBH, AG, KG, SA, NV,
  BV, OY, AB, KK, PTY, PTE`, the phrase `KABUSHIKI KAISHA`, and many more; organization keywords
  include `TRUST, BANK, UNIVERSITY, INSTITUTE, FOUNDATION, HOLDINGS, GROUP, TECHNOLOGIES, SYSTEMS,
  LABORATORIES, SEMICONDUCTOR, PHARMACEUTICALS, ELECTRONICS, COMMUNICATIONS, NETWORKS, INDUSTRIES,
  INTERNATIONAL, …`. Company signals win over person signals.
- **Individual** — the `LAST, FIRST [MIDDLE]` comma form (the dominant USPTO inventor format), or a
  short (2–4 token) all-alphabetic name with no company keyword.
- **Unknown** — everything else, e.g. single-token brands like `SONY` (deliberately not guessed).

**Optional ML backend** — set a step's *method* to `probablepeople` to use a CRF name classifier
(install with `pip install -e ".[ml]"`). If it isn't installed it logs a note and falls back to rules.

**Multi-party mode** (for concatenated `*_names` columns) — how to combine the parties' types:

| Mode | Result |
|---|---|
| `all` | one agreed type across every party, else `unknown` |
| `any` | `company` if any party is a company, else `individual` if any, else `unknown` |
| `first` | the first party's type |
| `majority` | the most common type (ties → `unknown`) |

---

## 9. Reference matching (PatentsView disambiguated assignees)

USPTO/PatentsView publishes **disambiguated assignee** data — raw assignee mentions already resolved
to canonical organization names plus a stable `assignee_id`. Used as a reference, it's an
authoritative **company gazetteer**: a raw **assignor** name that fuzzy-matches a disambiguated
assignee organization is a **known company** (kept and normalized); one that doesn't is a presumed
individual.

### Where to put the file

Put the file in the **`reference/`** folder (git-ignored, so large data never gets committed):

```
assignment_uspto/
  reference/
    g_assignee_disambiguated.tsv     ← the raw PatentsView download
    reference.parquet                ← the compact extract (recommended, see below)
```

The raw PatentsView file is tab-delimited with this header:

```
patent_id  assignee_sequence  assignee_id  disambig_assignee_individual_name_first
disambig_assignee_individual_name_last  disambig_assignee_organization  assignee_type  location_id
```

The [Match against reference](#match-against-reference) step's defaults already match it:

- **Reference name column** → `disambig_assignee_organization`
- **Reference id column** → `assignee_id` (optional; captures the stable entity id)
- **Delimiter** → auto-detected (`.tsv` → tab, `.csv` → comma, `.parquet` → Parquet)

Individual-assignee rows have an **empty** organization, so the distinct non-empty organizations form
a **company-only** gazetteer automatically — no `assignee_type` filter needed. (In the real file,
`assignee_type` is `2`/`3` for US/foreign company, `4`/`5` for individual, `6`–`9` for government.)

### Build a compact reference (recommended)

The raw TSV is multi-GB and gets re-scanned every run. In the reference-match dialog click **Build
compact…**, pick the big `.tsv`, and save `reference/reference.parquet`. The dialog then points at that
small file (columns `organization` + `assignee_id`), which reloads instantly. Within a single batch
run the reference is also cached in memory (loaded once).

### What the step outputs

For the matched column it adds:

- `<col>_disambiguated` — the raw name replaced by the matched organization (unmatched names kept as-is);
- `<col>_matched` — `"true"` / `"false"`;
- `<col>_assignee_id` — the matched organization's id (when an id column is set).

**Action**: `flag` (add the columns), `keep_matched` (keep only known-company rows — drops presumed
individuals), or `drop_matched`. **Mode**: `any` (matched if any party is a known company) or `all`.

---

## 10. Batch processing

*Settings ▸ Batch processing* opens the batch dialog. A **template** is a reusable pipeline:

```
BatchTemplate = name + LoadConfig + [ ordered list of steps ]
```

Each template is applied to **each input independently**. Inputs can be USPTO `.xml`/`.zip` files
**or** already-processed dataset folders (Arrow/Parquet).

**Building & running (left → right in the dialog):**

1. **Template** — name it; **Save** / **Delete**; reload a saved template from the dropdown.
2. **Inputs** — *Add files…* (`.xml`/`.zip`) and/or *Add folder…* (dataset folders); *Remove*.
3. **Load** — an optional max-record cap and a field/table selection tree (loads only what you need).
4. **Steps** — *Add step ▾* (menu below), reorder isn't needed (they run top-to-bottom);
   **double-click a step to edit** it; *Remove*.
5. **Output** — choose an output folder; **Workers** (1 = sequential; >1 processes files in parallel).
6. **Run batch** — watch the live **console**; a run log is written too.

**Output layout** — folder-per-source:

```
<output>/<template-name>/<source-stem>/<table>.<ext>
```

**Parallel runs** — with *Workers > 1* and multiple inputs, files are processed in separate
processes; the console shows distinct worker **PIDs**, interleaved per-file progress, and a combined
total. Sequential mode streams live per-step progress (`X of Y` for parse/normalize/classify/match).

**Performance built-ins:**

- **Skip-flat** — the parser builds only the tables your steps actually touch; if nothing needs the
  wide `flat` table it's skipped, roughly halving parse work.
- **Collision guard** — if two Normalize steps would write the same column on the same table, the
  second is auto-renamed (with a console note) so nothing is clobbered.
- **Learn-back** — aliases learned by workers are merged back into the shared entity memory after the
  run (persist the memory to keep learning across runs).

### Batch step catalog

Every step targets a `table` and (mostly) adds or transforms a column in place. Columns whose name is
left blank are **auto-derived** (shown below), so steps don't clobber each other.

#### Filter
Keep rows matching filter clauses (see [Filters](#6-filters)); optionally project columns and sort.
Fields: `table`, `clauses[]`, `combine` (`and`/`or`), `columns` (optional projection), `sort`.

#### Normalize
Fuzzy-normalize a name column to canonical forms via the entity memory.
Fields: `table`, `column` (default `name`), `target` (blank → `<column>_canonical`), `threshold`
(default 90), `separator` (blank → `"; "` for `*_names` columns), `learn` (default true — off =
match-only), `scorer` (default `wratio`; see [scorers](#7-entity-normalization--the-memory-editor)).

#### Classify
Add an entity-type column (`company`/`individual`/`unknown`).
Fields: `table`, `column`, `target` (blank → `<column>_type`), `method` (`rules`/`probablepeople`),
`mode` (`all`/`any`/`first`/`majority`), `separator`.

#### Compare
Compare two columns row-wise (e.g. assignor vs assignee) and flag or drop matches.
Fields: `table`, `left`, `right`, `target` (blank → `<left>_matches_<right>`), `method`
(`exact` — fast, ideal on canonical columns — or `fuzzy`), `scorer`, `threshold`, `action`
(`flag` = add a `"true"/"false"` column · `drop_matches` · `keep_matches`).

#### Transfer type
One-click preset: keep only rows whose classified assignor/assignee types match a chosen pairing.
Fields: `table` (default `flat`), `assignor_column` (`assignor_names`), `assignee_column`
(`assignee_names`), `assignor_type` (default `company`), `assignee_type` (default `company`),
`method`. Example: `company → company` keeps only firm-to-firm transfers.

#### Match against reference
Match a name column against a disambiguated-assignee reference file (see
[Reference matching](#9-reference-matching-patentsview-disambiguated-assignees)).
Fields: `table` (default `flat`), `column` (default `assignor_names`), `reference_path`,
`name_column` (default `disambig_assignee_organization`), `id_column` (optional, e.g. `assignee_id`),
`threshold`, `scorer`, `separator`, `mode` (`any`/`all`), `delimiter` (blank → auto), `action`
(`flag`/`keep_matched`/`drop_matched`). Outputs `<col>_disambiguated`, `<col>_matched`, and
`<col>_assignee_id` (when an id column is set).

#### Deduplicate
Drop duplicate rows (keep first). Fields: `table`, `subset` (key columns; blank → whole row).

#### Select columns
Keep and reorder a chosen set of columns. Fields: `table`, `columns[]`.

#### Sort
Order a table by a column (nulls last). Fields: `table`, `column`, `ascending`.

#### Derive column
Add a computed column. Fields: `table`, `source`, `target` (blank → `<source>_<op>`), `op`.
`op` values: `year` (first 4 chars of a `YYYYMMDD` date), `month` (chars 5–6), `split_first` (first
part of a `"; "`-joined value), `upper`, `lower`.

#### Aggregate (group & count)
Group by columns and count rows into a **new summary table**. Fields: `table`, `group_by[]`,
`count_distinct` (optional column to also count distinct values of), `out_table` (blank →
`<table>_by_<columns>`). Output has the group columns + `count` (+ `<col>_distinct`), sorted by count
descending. Exportable like any table.

#### Export
Write the current working tables (all, or a named subset) in one format.
Fields: `fmt` (`parquet`/`csv`/`xlsx`/`json`/`feather`), `tables` (blank = every table). Derived
tables (from Aggregate) are included in a bare export.

### Recipes

Each recipe is an ordered step list you build in the batch dialog (or a saved template JSON).

**A. Firm-to-firm transfers only**
- *Option 1 (composable):* Classify `assignor_names` → `assignor_names_type`; Classify
  `assignee_names` → `assignee_names_type`; Filter where `assignor_names_type = company` **AND**
  `assignee_names_type = company`.
- *Option 2 (preset):* a single **Transfer type** step, `company → company`.
- Then Export `flat`.

**B. Remove self-transfers (assignor == assignee)**
- Normalize `assignor_names` (scorer `token_set`); Normalize `assignee_names` (scorer `token_set`);
  **Compare** `assignor_names_canonical` vs `assignee_names_canonical`, method `exact`, action
  `drop_matches`; Export `flat`.

**C. Keep only assignors that are known companies (reference match)**
- Put `g_assignee_disambiguated.tsv` (or a **Build compact…** `reference.parquet`) in `reference/`.
- **Match against reference** on `assignor_names`, `id_column = assignee_id`, action `keep_matched`;
  Export `flat`. The kept rows have `assignor_names_disambiguated` + `assignor_names_assignee_id`;
  the dropped rows are presumed individuals.

**D. Top assignees by patent count**
- Normalize `assignees.name` → `name_canonical`; **Aggregate** `assignees` group by `name_canonical`
  → `assignees_by_name_canonical`; Export that table (Parquet/CSV).

---

## 11. Config & file locations

| What | Where |
|---|---|
| Entity memory (active) | `entities.json` in the working folder by default (relocatable) |
| Entity memory location pointer | `entity_location.json` under the OS app-config dir |
| Recent files | `recent.json` (config dir) |
| Saved queries | `queries.json` (config dir) |
| Saved batch templates | `batch_templates.json` (config dir) |
| Reference data | `reference/` (git-ignored) |
| CLI outputs | `out/` (default; git-ignored) |
| Batch outputs | your chosen output folder (`batch_output/` is git-ignored) |

The OS app-config dir is `<config>/uspto-assignment-viewer/` (falls back to `~/.config/…`).

---

## 12. Develop & quality gate

```bash
.venv/bin/ruff format . && .venv/bin/ruff check --fix .   # format + lint (line length 100)
.venv/bin/pyright                                          # type-check (strict)
.venv/bin/pytest -q                                        # tests (offscreen Qt for UI tests)
```

Layout: core package `src/uspto_assignments/` (Qt-free — `model`, `parser`, `tables`, `filters`,
`exporters`, `writers`, `normalize`, `classify`, `reference`, `batch`, `cli`) and the desktop app
`src/uspto_assignments_ui/` (imports the core, never the reverse). See `docs/UI_PLAN.md` for the UI
architecture and `CLAUDE.md` for contributor notes.

---

## Maintaining this guide

`GUIDE.md` is the **living user manual**. When you add or change a feature, update the relevant
section here in the same change — most importantly:

- the [Batch step catalog](#batch-step-catalog) (new steps, fields, defaults, enum values), and
- the [Recipes](#recipes) (keep them runnable).

Keep the exact CLI flags, operator/scorer/format lists, and menu labels in sync with the code so this
stays authoritative.
