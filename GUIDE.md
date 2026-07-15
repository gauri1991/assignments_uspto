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
11. [Troubleshooting & known issues](#11-troubleshooting--known-issues)
12. [Config & file locations](#12-config--file-locations)
13. [Develop & quality gate](#13-develop--quality-gate)
14. [Maintaining this guide](#maintaining-this-guide)

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
| `ml` | `probablepeople` | alias for the ML classifier — now part of the default install (rules still work if absent) |
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
.venv/bin/uspto-assign templates-summary            # regenerate templates/TEMPLATES.md
```

`parse` and `ingest` write a `manifest.json` audit record into the output directory (command,
input, duration, every output file with rows). `templates-summary` renders every template as
numbered step one-liners plus its validation warnings into `templates/TEMPLATES.md` — regenerate
it after any template change (run from the project root so relative reference paths validate).

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

**Landing page** — tiles to *Open XML / ZIP*, *Open dataset folder*, or
*View Parquet / data file*, plus a **Recent** grid (hidden until you've opened something).

**View / edit / convert any data file** — *View Parquet / data file…* (landing tile or the File
menu) opens a single **`.parquet` / `.arrow` / `.feather` / `.csv`** file as a one-table view. All
values are shown as text (list columns joined with `"; "`). From there the whole toolkit applies:
**filter / sort / paginate**, **Edit columns…** (keep / reorder / rename / drop columns), and
**Export current table…** to convert it to **CSV / JSON / Excel / Parquet / Feather**. This is how
you inspect a batch run's per-step trace files (`steps/NN_<table>.parquet`) or any Parquet from
another tool, and reshape or convert it without leaving the app.

**Menus & toolbar** (exact labels):

- **File**: `Open XML/ZIP…` (Ctrl+O) · `Open dataset folder…` · `View Parquet / data file…` ·
  `Edit columns…` · `Save processed…` (Ctrl+S) ·
  `Export current table…` (Ctrl+E) · `Export all tables…` · `Close dataset` (Ctrl+W) · `Exit`.
- **Queries**: `Save current query…` · `Manage queries…`.
- **Settings**: `Batch processing…` (Ctrl+B) · `Entity memory…` · `CPC / USPTO API data source…`.
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

### Match confidence & the review band

The threshold is a hard gate — but a match that scored exactly 90 is not the same as a score-100
exact hit. The fuzzy steps (**Normalize**, **Match against reference**, **Compare**) can keep that
confidence visible instead of discarding it:

- **Add match-score column** — emits `<target>_score` (0–100). `100` = exact or identity, `0` = no
  match; for multi-party values (split on `"; "`) it is the **weakest** party's score, since one bad
  party is what makes a row suspect.
- **Flag review below N** — emits `<target>_review` with `"true"` for rows that were **accepted via
  a fuzzy match scoring under N**. Exact matches (100) and unmatched rows (0) never flag, whatever
  the threshold. This is the classic record-linkage *clerical-review band*: accept ≥ N outright,
  review [threshold, N), reject < threshold. A typical setup is threshold 90, review below 95.

Both columns flow through the schema-aware pickers, filters, and exports like any other column — so
you can filter `<target>_review = true` into its own worksheet, or sort an export by score. Scores
also have **provenance**: an alias learned from a marginal fuzzy match remembers its original score,
so on later runs the exact alias hit still reports 90, not a laundered 100.

### The learnable entity memory

Normalization is backed by an **entity memory**: a deduplicated set of canonical names plus learned
`alias → canonical` mappings. It **grows as it resolves names** (unless a step sets *match-only*), and
persists to JSON. It's stored in a **relocatable project file** (default `entities.json` in the
working folder); a pointer file remembers the chosen location across sessions.

Each fuzzy-learned alias is stored **with the score it was learned at** (file format v2:
`"alias": ["CANONICAL", 91]`; curated/exact entries stay plain strings, and v1 files load unchanged
as score-100). The store is only rewritten when a run actually learned something.

### The memory editor — *Settings ▸ Entity memory*

A tabbed editor that works on a **working copy** (Save persists, Cancel discards):

- **Canonicals** tab — searchable list; **Add**, **Rename** (repoints its aliases), **Merge** (fold
  one canonical into another), **Delete** (also removes its aliases). Structural edits rebuild the
  fuzzy block index so matching keeps working. Each canonical also shows its **entity type** inline
  (`ACME INC · company`) when tagged:
  - **Tag all…** classifies every canonical as company / individual / unknown using **Rules** (fast,
    deterministic) or **ML (probablepeople)** — chosen when you click, and run off the UI thread.
  - **Set type…** overrides the type of the selected canonical(s) by hand (multi-select supported).
  - **Seed from reference…** tags every seeded organization `company` automatically (a disambiguated
    gazetteer is companies by definition).
  - Types are stored in the entities JSON (`"types"`) and persist on **Save**. A [normalize
    step](#batch-step-catalog) with **emit type** reuses these tags to add a `<target>_type` column
    downstream — so tag once, then filter (e.g. `_type equals company`) across every run.
- **Aliases** tab — searchable table of `alias → canonical` with a **Score** column (the confidence
  each alias was learned at; 100 = exact or curated). **Double-click a canonical cell to reassign**
  an alias, or **Delete alias**.
- **The review queue** — check **Only aliases learned below [N]** to see just the marginal fuzzy
  learnings. For each one either **Mark reviewed** (accept it: score becomes 100 and it leaves the
  queue), double-click to reassign it to the right canonical (also marks it human-confirmed), or
  **Delete alias**. Work the queue down after big normalize runs.
- File actions — **Import…** (seed from CSV `alias,canonical` / JSON / one-name-per-line),
  **Seed from reference…** (stream a disambiguated-assignee file — TSV/CSV/Parquet, multi-GB OK —
  and add every distinct organization as a canonical; see
  [Reference matching](#9-reference-matching-patentsview-disambiguated-assignees)), **Export…**
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

**ML backend** — set a step's *method* to `probablepeople` to use a CRF name classifier. It's
included in the default install, so this works out of the box; if the package is ever missing the
run log shows a one-line note (amber) and the step falls back to rules.

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

The raw TSV is multi-GB and gets re-scanned every run, so extract it **once** into a small
`reference/reference.parquet` (columns `organization` + `assignee_id`) that reloads instantly.
The column names are **auto-detected** — you don't need to know or type them.

**From the command line** (simplest):

```
.venv/bin/uspto-assign build-reference reference/g_assignee_disambiguated.tsv
```

That writes `reference/reference.parquet` by default. Override the destination with `--out`, or the
columns with `--name-column` / `--id-column` if your file uses unusual headers (pass
`--id-column ""` to omit ids). It prints e.g. `reference built: 516,032 distinct organizations`.

**From the app:** in the reference-match dialog click **Build compact…**, pick the big `.tsv`, and
save `reference/reference.parquet`. The dialog then points at that small file and fills in its
`organization` / `assignee_id` columns for you. (You can leave the column fields blank — the build
auto-detects them, and even forgives a wrong name.) Within a single batch run the reference is also
cached in memory (loaded once).

### What the step outputs

For the matched column it adds:

- `<col>_disambiguated` — the raw name replaced by the matched organization (unmatched names kept as-is);
- `<col>_matched` — `"true"` / `"false"`;
- `<col>_assignee_id` — the matched organization's id (when an id column is set);
- `<col>_match_score` — the weakest matched-party score, 0–100 (only with *Add match-score column*);
- `<col>_match_review` — `"true"` for rows accepted below the review bar (only with *Flag review
  below N* — see [Match confidence & the review band](#match-confidence--the-review-band)).

**Action**: `flag` (add the columns), `keep_matched` (keep only known-company rows — drops presumed
individuals), or `drop_matched`. **Mode**: `any` (matched if any party is a known company) or `all`.

### 9.5 CPC matching (fetch → rank buyers)

> **See [`CPCmatching.md`](CPCmatching.md)** for the complete CPC reference: all CPC templates, how
> matching works, the fully offline **13 → 14** chain, the many-to-many **`matched_cpc_classes`** table,
> file formats, and config knobs. This section is the quick version.

The final step of the strategy — matching a **sales package** (a portfolio of patents you're selling)
to the buyers most likely to want it — is done with two dedicated batch steps: **Fetch CPC** and
**CPC match**. These are *not* the fuzzy `reference_match` step: attaching CPC and computing overlap
are **exact joins on the normalized grant number**, so they get their own step kinds (pointing
`reference_match` at patent numbers would silently corrupt them).

**Configure the source once — *Settings ▸ CPC / USPTO API data source*.** The dialog edits a project config file
(`cpc_config.json`, saved in the working folder, shareable — it holds **no secret**) covering:

- **Source**: **USPTO ODP / PatentSearch API** (default) or a **local bulk file**
  (e.g. PatentsView `g_cpc_current.tsv`, offline & scale-safe). The live API is the USPTO Open Data
  Portal Patent Search endpoint `https://api.uspto.gov/api/v1/patent/applications/search`,
  authenticated with an `X-API-KEY` header (PatentsView migrated to `data.uspto.gov` on
  2026-03-20; the old `search.patentsview.org` endpoint is dead and auto-repaired on load).
- **API key**: stored **only as an environment-variable name** (default `USPTO_ODP_API_KEY`); the key
  itself is read from the environment at run time and never written to disk. The dialog shows whether
  that variable is currently set, and a **Test connection** button fires one live call (patent
  10000000) to confirm the key + endpoint actually work, reporting the CPC codes it got back.
- **Network posture**: **offline by default**. A `fetch_cpc` step reads the cache only and lists
  uncached numbers; it hits the network **only** when all three switches are on: `export
  USPTO_ODP_API_KEY=…` (add it to `~/.bashrc` to persist), set **Network posture = Allow network
  fetch**, and tick *Allow network for CPC fetch this run* in the batch dialog. After the first
  fetch populates the cache (`data/cpc/`, TTL-controlled), every run is offline and reproducible.
- **Match defaults**: overlap grain (`subclass` default / `main_group` / `full_symbol`), overlap
  metric (`shared_count` / `jaccard` / `rarity_weighted`), threshold, ranking weights, minimum
  in-domain patents, and the hit-rate floor. Keeping these in the config (not per-step) means every
  CPC template stays consistent.

**The two-template workflow** (matches how the front-half bridge is built):

1. **Enrich**: run your firm-to-firm / recency / buyer-resolution template, add a **Fetch CPC** step
   on the patent-number column, and export — you now have buyers × patents with CPC attached.
2. **Match**: a separate template with a **CPC match** step that reads that enriched data plus your
   sales-package portfolio (a **patent-number list** whose footprint is resolved via the same
   source/cache, or a **pre-built `patent,cpc` footprint file** for hand-tuning). It emits
   `matched_buyers_by_portfolio_patent` (per portfolio patent → ranked buyers: overlap strength,
   in-domain patent count, last acquisition date, off-gazetteer flag) and `matched_buyers_overall`.

Two ready-to-import example templates ship for exactly this: **`08_cpc_enrich_firm_to_firm.json`**
(rules-only firm-to-firm + Fetch CPC → Parquet export, no reference file needed) and
**`09_cpc_match_to_portfolio.json`** (CPC match on 08's output → ranked-buyer CSVs). Import them via
*Settings ▸ Batch processing*, run 08 on your XML/ZIP, then run 09 with its input pointed at 08's
output folder and its `portfolio_path` set to your portfolio file.

**Number-format discipline (why hit-rate matters).** CPC is assigned at **grant**, so application and
publication numbers resolve to nothing — both steps route to grants via `kind_column` and normalize to
the bare grant number the CPC source uses. A **low `cpc_hit_rate`** means the number formats are
misaligned, *not* that buyers have no CPC — so `cpc_match` **aborts** below the configured floor rather
than returning a misleading empty result.

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
2. **Inputs** — *Add files…* (multi-select `.xml`/`.zip`) and/or *Add folder…*; *Remove*. **Add
   folder…** is smart: point it at an already-parsed **dataset folder** (has `flat.parquet` /
   `flat.arrow` …) and it's added as one dataset input; point it at any **other folder** and it's
   scanned **recursively** for every `.xml`/`.zip`, each added as its own input — so a whole folder
   of daily/annual dumps queues in one click.
3. **Load** — an optional max-record cap and a field/table selection tree (loads only what you need).
4. **Steps** — *Add step ▾* (menu below), reorder isn't needed (they run top-to-bottom);
   **double-click a step to edit** it; *Remove*.
5. **Output** — defaults to your last-used folder (or `data/out`); **Workers** (1 = sequential;
   >1 processes files in parallel). File dialogs remember where you last picked.
   **Save each step's output** (checkbox) writes every enabled step's resulting table to
   `<source>/steps/NN_<table>.parquet` so you can open and check each intermediate — see below.
6. **Run batch** — watch the live **console** and the per-file progress bar; a run log is written
   too. A **Cancel** button appears while a run is active (cancellation is per-file: the file in
   flight finishes, the rest are skipped, and the summary notes what was cancelled). Closing the
   window mid-run prompts to cancel first — the window closes itself once the run stops.

**Output layout** — every run gets its own self-contained, audit-ready folder:

```
<output>/
├── runs_index.csv    ← one appended line per run, across all templates
└── <template-name>/run_<timestamp>/
    ├── manifest.json     ← full audit record: template + step summaries, warnings,
    │                        per-file & per-step row counts, output paths
    ├── summary.xlsx      ← the same record as a workbook (run / steps / outputs sheets)
    ├── run.log           ← plain-text per-file results
    └── <source-stem>/<table>.<ext>
```

Re-running never mixes with earlier outputs (a duplicate timestamp gets a `` (1)`` suffix).

**Save each step's output (step-by-step review).** Tick the checkbox to trace the pipeline: every
enabled step's resulting table(s) are written under each source's `steps/` folder as
`NN_<table>.parquet` (`NN` = step number), so you can open any intermediate and check exactly what
that step produced — filter → normalize → match, one file each:

```
<source-stem>/
├── steps/
│   ├── 01_flat.parquet        ← after step 1 (e.g. Filter)
│   ├── 02_flat.parquet        ← after step 2 (e.g. Normalize)
│   └── 03_flat.parquet        ← after step 3 (e.g. Attach CPC)
└── <table>.<ext>              ← the final Export outputs
```

The files are **Parquet** (lossless, list columns like `cpc_codes` intact) — open any of them in the
app with *View Parquet / data file…* to inspect that step's output (filter/sort, or convert it to
CSV/Excel). Steps that create a new table
(Aggregate, CPC match) trace that table too; the Export step writes the real exports, not a trace.
The manifest lists every trace file. Leave the box **off** for normal runs — tracing multiplies the
files written, so it's meant for reviewing/validating on a **shortlisted** set, not bulk runs.

**Convert mode (one folder, files named by source).** Tick **Convert mode: one folder, files named
by source** to bypass the timestamped run folder entirely: outputs land **directly in the folder you
picked**, named `<source-stem>_<table>.parquet`, all side by side. No per-source subfolder, no
`manifest.json` / `summary.xlsx` / `runs_index.csv`. A re-run **overwrites** same-named files (two
inputs that share a stem get a `` (1)`` suffix so they never clobber each other). This is the
fast path for bulk **XML/ZIP → Parquet** conversion — pair it with the bundled **12 - Convert to
Parquet** template (a single Export-Parquet step, all 5 tables):

```
<chosen folder>/
├── ad20260101_assignments.parquet
├── ad20260101_assignors.parquet
├── ad20260101_assignees.parquet
├── ad20260101_properties.parquet
├── ad20260101_flat.parquet
├── ad20260102_assignments.parquet
└── …
```

Workflow: **Settings ▸ Batch ▸ Import…** template 12 → **Add files…** (multi-select `.xml`/`.zip`)
→ pick the output folder → tick **Convert mode** → **Run**. (Step tracing is ignored in convert mode.)

**Parallel runs** — with *Workers > 1* and multiple inputs, files are processed in separate
processes; the console shows distinct worker **PIDs**, interleaved per-file progress, and a combined
total. Sequential mode streams live per-step progress (`X of Y` for parse/normalize/classify/match).

**Performance built-ins:**

- **Skip-flat** — the parser builds only the tables your steps actually touch; if nothing needs the
  wide `flat` table it's skipped, roughly halving parse work.
- **Collision guard** — if two Normalize steps would write the same column on the same table, the
  second is auto-renamed (with a console note) so nothing is clobbered.
- **Learn-back** — aliases learned by workers are merged back into the shared entity memory after the
  run, with their match scores. The memory file is only rewritten when a run actually learned
  something.

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
match-only), `scorer` (default `wratio`; see [scorers](#7-entity-normalization--the-memory-editor)),
`emit_score` (adds `<target>_score`), `review_threshold` (0 = off; adds `<target>_review` — see
[Match confidence](#match-confidence--the-review-band)).

#### Classify
Add an entity-type column (`company`/`individual`/`unknown`).
Fields: `table`, `column`, `target` (blank → `<column>_type`), `method` (`rules`/`probablepeople`),
`mode` (`all`/`any`/`first`/`majority`), `separator`.

#### Compare
Compare two columns row-wise (e.g. assignor vs assignee) and flag or drop matches.
Fields: `table`, `left`, `right`, `target` (blank → `<left>_matches_<right>`), `method`
(`exact` — fast, ideal on canonical columns — or `fuzzy`), `scorer`, `threshold`, `action`
(`flag` = add a `"true"/"false"` column · `drop_matches` · `keep_matches`), `emit_score` (adds
`<target>_score`, the per-row similarity — added before any drop/keep filtering so surviving rows
keep it), `review_threshold` (0 = off; adds `<target>_review` flagging fuzzy matches below the bar).

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
(`flag`/`keep_matched`/`drop_matched`), `emit_score` (adds `<col>_match_score`),
`review_threshold` (0 = off; adds `<col>_match_review`). Outputs `<col>_disambiguated`,
`<col>_matched`, and `<col>_assignee_id` (when an id column is set).

#### Fetch CPC
Attach CPC classification codes to a patent-number column via the configured CPC source
(see [CPC matching](#95-cpc-matching-fetch--rank-buyers)). Fields: `table` (default `flat`),
`column` (patent-number column, default `doc_number`), `kind_column` (default `doc_kind`, used to
route to grants — CPC is grant-only). Outputs `cpc_codes`, `cpc_subclasses`, `cpc_lookup_status`.
Source/cache/offline behavior come from the project CPC config, **not** the step. This is an **exact**
join on the normalized grant number — deliberately *not* the fuzzy `reference_match` step (which,
pointed at patent numbers, would silently corrupt them). **Offline by default**: uncached numbers are
fetched only when *Allow network* is checked for the run.

#### Attach CPC from file
Same output as *Fetch CPC* (`cpc_codes`, `cpc_subclasses`, `cpc_lookup_status`), but the codes come
from an **uploaded file** — a PatSeer export, or any CSV/TSV/Parquet of patent→CPC — instead of the
API. **Fully offline, no cache, no key.** Fields: `table`, `column` (the table's patent-number
column) + `kind_column`, `source_path` (browse to the file), `patent_column` (the file's
patent-number column, default `Publication Number`), `code_column` (the file's CPC column, default
`CPC`), and `separator` — set it (e.g. `;` or `|`) to split a cell that packs **several CPC codes**
(typical PatSeer), or leave it blank for one code per row (USPTO `g_cpc_current` bulk layout). The
join normalizes the same grant numbers as *Fetch CPC*, so it's exact and grant-only. Ideal after
shortlisting records — attach CPC from a targeted export without touching the network.

#### CPC match
Match a sales-package portfolio against buyers' CPC footprints and rank buyers per portfolio patent
(run a Fetch CPC step first). Fields: `table`, `portfolio_mode` (`patent_list` | `footprint_file`),
`portfolio_path`, `buyer_column` (default `assignee_names_canonical`), `number_column`, `kind_column`,
`date_column` (default `transaction_date`). All match knobs (grain, overlap metric/threshold, ranking
weights, hit-rate floor) come from the project CPC config. Produces two tables:
`matched_buyers_by_portfolio_patent` (per patent → ranked buyers) and `matched_buyers_overall`
(cross-portfolio summary). **Aborts** if the CPC hit-rate is below the floor (a patent-number-format
mismatch, not "no data").

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

> **Use `transaction_date` as the date `source`, not `recorded_date`.** `recorded_date` is when the
> USPTO *recorded* the transfer and lags the real event; `transaction_date` is the latest assignor
> `execution_date`, falling back to `recorded_date` only when no signer date was filed (with
> `date_source` recording which). It is the true-event axis and is always populated. The bundled
> firm-to-firm / CPC templates derive their `year` from `transaction_date` for exactly this reason —
> match that when you build your own.

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

**E. CPC portfolio match — rank buyers for a sales package (two templates)**

The end-to-end "who should I sell this patent to?" flow. It ships as two importable templates
(`08_cpc_enrich_firm_to_firm.json` and `09_cpc_match_to_portfolio.json`); the steps below are what
they contain, so you can also build them by hand. See [CPC matching](#95-cpc-matching-fetch--rank-buyers)
for the config.

*One-time setup:* open *Settings ▸ CPC / USPTO API data source*. Either set **Source = Local bulk file** and point
it at a PatentsView `g_cpc_current.tsv` (fully offline), **or** keep **Source = USPTO ODP / PatentSearch
API** and `export USPTO_ODP_API_KEY=…` in your shell. Import both templates via
*Settings ▸ Batch processing*.

1. **Template 08 — enrich (raw file → CPC-tagged buyers).** Steps: **Filter** the firm-to-firm
   conveyance/date gate · **Transfer type** `company → company` · **Normalize** `assignor_names` and
   `assignee_names` · **Compare** the two `*_canonical` columns, `drop_matches` (remove self-transfers)
   · **Derive** `year` from `transaction_date` · **Fetch CPC** on `doc_number` (kind `doc_kind`) ·
   **Export** `flat` as **Parquet**. Run it on your `.xml`/`.zip` — if you're using the API source,
   tick **Allow network for CPC fetch this run** so uncached grants are fetched (and cached).
2. **Template 09 — match (enriched data + portfolio → ranked buyers).** One **CPC match** step
   (`buyer_column = assignee_names_canonical`, `number_column = doc_number`) plus an **Export** of
   `matched_buyers_by_portfolio_patent` and `matched_buyers_overall` as CSV. Set the step's
   `portfolio_path` to your sales-package file — a plain **`.txt` of grant numbers** (one per line;
   footprints resolved via the same source/cache) or a pre-built **`patent,cpc` CSV**. Point the run's
   **input at Template 08's output folder** (the enriched dataset) and run.

Output: per portfolio patent, a ranked buyer shortlist (overlap strength, in-domain patent count, last
acquisition date, off-gazetteer flag), plus a cross-portfolio buyer summary. Tune grain / metric /
threshold / ranking weights in *Settings ▸ CPC / USPTO API data source*. A low `cpc_hit_rate` **aborts** the match
(a patent-number-format mismatch — see [Troubleshooting](#11-troubleshooting--known-issues)).

**F. Attach CPC from a PatSeer/CSV export — offline, no API (template `11`)**

When you already have CPC codes in a spreadsheet (a PatSeer export, or any patent→CPC CSV/TSV/Parquet),
the **Attach CPC from file** step joins them on without the network. Ships as
`11_attach_cpc_from_file.json`:

1. **Filter** the firm-to-firm gate **and** `doc_kind = B2` (CPC is grant-only, so shortlist to grants
   first — this is the "shortlist, then CPC" pattern).
2. **Attach CPC from file** on `doc_number`: browse to your export, set **File patent column**
   (`Publication Number`), **File CPC column** (`CPC`), and the **separator** — `;` splits a cell that
   packs several CPC codes (typical PatSeer), blank = one code per row. Adds
   `cpc_codes`/`cpc_subclasses`/`cpc_lookup_status`, exactly like *Fetch CPC* but fully offline.
3. **Derive** `year`, then **Export** the CPC-tagged rows.

Put your export where the step points (`cpc/patseer_export.csv`) or re-point the step; no key, no
network, no cache. Ideal after you've filtered down to the records you care about. Tip: turn on
**Save each step's output** to drop each step's table into `steps/` and eyeball the join.

---

## 11. Troubleshooting & known issues

### "step N (filter) left '`<table>`' EMPTY — check the filter clause, reference_path/name_column, or the match gate"

A step reduced a **non-empty** table to **zero rows**. It's a red console warning, not a crash — the
pipeline keeps running but every later step (and the export) now has nothing to work on. The message is
generic; for a **filter** step, only "check the filter clause" applies.

**Most common cause — the firm-to-firm step-1 filter matches nothing in your file.** The bundled
firm-to-firm / CPC templates open with a filter that **AND**s four conditions, so a row survives only
if *all* hold:

1. `conveyance_text` **contains** `ASSIGNOR'S INTEREST`
2. `assignee_names` **not empty**
3. `purge_indicator` **≠** `Y`
4. `recorded_date` **not empty**

The usual culprit is **condition 1**. Real USPTO sales read `ASSIGNMENT OF ASSIGNOR'S INTEREST` —
**with an apostrophe** (`ASSIGNOR'S`). The templates match that phrase; a file holding only
**security interests, mergers, name changes, or government-interest** records contains no
`ASSIGNOR'S INTEREST` text, so the AND drops everything. (Note: earlier template versions searched
for `ASSIGNORS INTEREST` *without* the apostrophe, which matched almost nothing — if you edited a
copy of a template, make sure the clause has the apostrophe.)

**How to diagnose (30 seconds):** open the same `.xml`/`.zip` normally (not batch), add a filter clause
on **`conveyance_text`** — because it's low-cardinality the value box pre-fills the **distinct
conveyance types actually in your file**. If none contain "ASSIGNOR'S INTEREST", your file has no
firm-to-firm sale records under that phrasing. While there, check `recorded_date` isn't blank across the
file (that would trip condition 4). You can also use **Batch ▸ Preview** — it runs the pipeline on a
~1,000-row sample and shows each step's row delta, pinpointing which step zeroed the table.

**Fixes:**
- **Relax condition 1** — double-click the filter step and change the clause to a phrase your file
  actually uses (e.g. just `ASSIGNOR`, or the specific conveyance type you want), or remove it.
- Accept that the file genuinely has **no** firm-to-firm records — then a firm-to-firm template can't
  produce output regardless.

> The AND combine is strict: **every** clause must pass. If you meant "any of these", switch the step's
> combine toggle to **OR** (*Match any*). See [Filters](#6-filters).

### "reference file not found" — templates that use *Match against reference*

The bundled buyer templates **01, 02, 03, 04, 05, 07** (and buyer_identification T1) include a
[*Match against reference*](#match-against-reference) step pointed at **`reference/reference.parquet`**.
That path is **git-ignored** and **not shipped**, so on a fresh clone (or a machine that only has the
raw PatentsView `.tsv`) the step fails and its table empties.

**Fixes — pick one:**
- **Build the compact reference once.** Put the PatentsView `g_assignee_disambiguated.tsv` in a
  `reference/` folder at the project root, open the *Match against reference* dialog, click
  **Build compact…**, select the `.tsv`, and save `reference/reference.parquet`. All the templates
  point at that file. See [Reference matching](#9-reference-matching-patentsview-disambiguated-assignees).
- **Use the rules-only template `06`** (its reviewed-bundle twin is `reviewed.json` **T5**). It replaces
  gazetteer matching with a rules-based *Transfer type* step, so it needs **no reference file at all** —
  the fastest path when you don't need gazetteer-accurate entity resolution.

> On Windows: create the `reference/` folder yourself (it's the only git-ignored input you must supply —
> output folders like `out/`, `dictionary/` are auto-created). See the README's Windows section.

### "reference file has no column '…'" — reference/step column mismatch

Pre-run validation now checks that the reference file **actually contains** the configured
`name_column`/`id_column`. The classic trigger: a compact `reference.parquet` built via
**Build compact…** with the *Reference id column* field left blank has only an `organization`
column, while the bundled templates configure `id_column: "assignee_id"`. Running anyway fails
that file with a clear error naming the missing column and the columns the file does have.

**Fixes:** rebuild the compact file with the id column filled in (`assignee_id`) — recommended,
since the id powers the id-based self-transfer check — or clear the step's *Reference id column*
to skip ids.

### Reviewing marginal fuzzy matches (the review queue)

Fuzzy accepts are not all equal: a name that scored 90 against the gazetteer deserves a look; a
100 doesn't. Turn on **Add match-score column** and **Flag review below 95** on the fuzzy steps
(see [Match confidence](#match-confidence--the-review-band)), filter `*_review = "true"` in the
output, and work the **Entity memory ▸ Aliases** review queue (*Only aliases learned below N* →
**Mark reviewed** / reassign / delete). Template **10 - Dropped sellers audit** is the ready-made
audit view of what template 01's gazetteer gate excluded.

### `cpc_match` aborts with "CPC hit-rate … below the floor"

The patent numbers and your CPC source's key format don't line up, so almost nothing joined — the
guard stops rather than hand you a misleadingly empty result. The CPC source's patent id should be a
**bare grant number** (e.g. `10987654` — no country prefix, no kind suffix, no leading zeros), which
is what `fetch_cpc` normalizes to. Check the source's patent-id column/format (*Settings ▸ CPC data
source ▸ File patent column*), and confirm you're pointing at a CPC table, not an application table.

### "N grant patents are uncached — enable network to fetch"

`fetch_cpc` is **offline by default**: it found grant patents with no cached CPC and didn't call the
API. To fetch and cache them, turn on all three switches: `export USPTO_ODP_API_KEY=…`, set
**Network posture = Allow network fetch** in *Settings ▸ CPC / USPTO API data source*, and tick
***Allow network for CPC fetch this run*** in the batch dialog. Subsequent runs read from the cache
offline. Or point the source at a local bulk CPC file, which never needs the network. Use the
dialog's **Test connection** button first to confirm the key + endpoint work.

### CPC fetch fails with a connection or HTTP 4xx error

The live endpoint is `https://api.uspto.gov/api/v1/patent/applications/search` (PatentsView moved to
the USPTO Open Data Portal on 2026-03-20; the old `search.patentsview.org` host is gone — a saved
config still pointing there is auto-repaired on load). An **HTTP 401/403** means the key is
missing or wrong: check that `USPTO_ODP_API_KEY` is exported and valid (the **Test connection**
button reports the exact status). Get a key from <https://data.uspto.gov>. A **name-resolution /
timeout** error means no network path to `api.uspto.gov` — fetch is skipped and rows stay
`uncached`.

### The exported `year` column is blank for some rows

The bundled templates derive `year` from **`transaction_date`** (the true-event date — see the note
below). `transaction_date` is `execution_date` when the filing recorded one, else `recorded_date`, so
it's populated for almost every row; a blank `year` means the record had **neither** an execution date
**nor** a recorded date. The exported **`date_source`** column tells you which basis each row used
(`execution` vs `recorded`).

---

## 12. Config & file locations

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

## 13. Develop & quality gate

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
