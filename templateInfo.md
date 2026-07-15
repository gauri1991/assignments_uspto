# Batch Template Authoring Spec (for generating importable pipeline templates)

This document fully specifies the **JSON format** of a batch-processing template for the USPTO
patent-assignment tool. Hand this file to an assistant (e.g. claude.ai) and ask it to produce a
`.json` file of one or more templates; then in the app open **Settings ▸ Batch processing ▸
Import…** and select the file. Each imported template can be run, previewed, and saved.

> **Golden rule for the author:** a template is a JSON **array** of template objects. It must import
> cleanly via the app's Import button. Only use the table names, column names, step `kind`s, and enum
> values listed here. Unknown keys are ignored; omitted optional fields take their defaults.

---

## 1. File shape

```json
[
  { "name": "My template", "load": { "limit": null, "columns": {} }, "steps": [ /* step objects */ ] },
  { "name": "Another template", "load": {}, "steps": [ /* ... */ ] }
]
```

- Top level is a **JSON array** (even for a single template).
- Each **template object** has three keys:
  - `name` (string, required) — shown in the app.
  - `load` (object, optional) — how much/what to load before the steps run (see §3).
  - `steps` (array, required) — the ordered pipeline (see §5). Steps run **top to bottom**; each
    transforms the working tables in place.

Encoding: UTF-8, standard JSON (no comments, no trailing commas).

---

## 2. Tables and their columns

Parsing produces **five tables**. Use these exact names and column names. `flat` is the
denormalized, analysis-ready table (one row per patent property) and is what most pipelines use.

| Table | Columns |
|---|---|
| `assignments` | `reel_no`, `frame_no`, `last_update_date`, `recorded_date`, `purge_indicator`, `page_count`, `conveyance_text`, `correspondent_name`, `correspondent_address_1`, `correspondent_address_2`, `correspondent_address_3`, `correspondent_address_4` |
| `assignors` | `reel_no`, `frame_no`, `name`, `execution_date`, `date_acknowledged` |
| `assignees` | `reel_no`, `frame_no`, `name`, `address_1`, `address_2`, `city`, `state`, `country_name`, `postcode` |
| `properties` | `reel_no`, `frame_no`, `invention_title`, `doc_country`, `doc_number`, `doc_kind`, `doc_date`, `doc_name` |
| `flat` | `reel_no`, `frame_no`, `last_update_date`, `recorded_date`, `purge_indicator`, `page_count`, `conveyance_text`, `correspondent_name`, `correspondent_address_1..4`, `assignor_names`, `assignee_names`, `assignor_count`, `assignee_count`, `execution_date`, `date_acknowledged`, `transaction_date`, `date_source`, `invention_title`, `doc_country`, `doc_number`, `doc_kind`, `doc_name`, `doc_date` |

> **`execution_date` / `date_acknowledged` on `flat`** roll up the assignors' dates (the **latest**
> signer date = the effective transaction date). Prefer `execution_date` over `recorded_date` for
> time trends — `recorded_date` is when the USPTO *recorded* the transfer and lags the real event
> (worst for late-recorded litigation cleanups / distressed sales). **`transaction_date`** does that
> fallback for you: it is `execution_date` when present, else `recorded_date`, with **`date_source`**
> (`execution` / `recorded`) recording which — so a `derive year` off `transaction_date` gives an
> always-populated, true-event time axis. Use `transaction_date` for time trends.

Facts the author must respect:

- **All values are strings.** Dates are `YYYYMMDD` text (e.g. `"20240115"`), so they compare/range
  **lexicographically** (`"20060101" ≤ x ≤ "20261231"` works). Partial dates like `"20240000"` occur.
- On `flat`, `assignor_names` / `assignee_names` are **multi-party** values joined with `"; "`
  (e.g. `"SMITH, JOHN; ACME INC"`). Steps that operate on them split on `"; "` automatically.
- The `assignors`/`assignees` tables use the singular column **`name`** (not `assignor_names`).

### 2a. Columns ADDED by steps (schema evolves down the pipeline)

Later steps may reference columns produced by earlier steps. The names are **derived** and
predictable — an author must know these so a Filter/Select/Sort/Aggregate/Export placed *after* a
producing step can reference them:

| Step | New column(s) on its `table` |
|---|---|
| `normalize` on `column` | `<column>_canonical`; with `emit_score`: `<target>_score` (int 0–100); with `review_threshold>0`: `<target>_review` (`"true"`/`"false"`); with `emit_type`: `<target>_type` (`company`/`individual`/`unknown`/`""`, from the entity memory's stored tags) |
| `classify` on `column` | `<column>_type` (values `company` / `individual` / `unknown`) |
| `derive` from `source` with `op` | `<source>_<op>` |
| `compare` with `action: "flag"` | `<left>_matches_<right>` (values `"true"` / `"false"`); with `emit_score`: `<target>_score`; with `review_threshold>0`: `<target>_review` (added before any drop/keep filtering) |
| `reference_match` on `column` | `<column>_disambiguated`, `<column>_matched` (`"true"`/`"false"`), `<column>_assignee_id` (only if `id_column` set); with `emit_score`: `<column>_match_score`; with `review_threshold>0`: `<column>_match_review` |
| `fetch_cpc` / `attach_cpc_file` | `cpc_codes` (list), `cpc_subclasses` (list), `cpc_lookup_status` |
| `cpc_match` | creates **two new tables**: `matched_buyers_by_portfolio_patent` and `matched_buyers_overall` |
| `aggregate` | creates a **new table** named `<table>_by_<group_by joined by _>` with columns = the group-by columns + `count` (+ `<count_distinct>_distinct` if set) |

Example: after `{"kind":"normalize","table":"flat","column":"assignor_names"}`, the column
`assignor_names_canonical` exists on `flat` and can be filtered/exported by later steps.

> Steps that transform in place but **don't** add columns: `filter` (may project via its `columns`),
> `select`, `sort`, `dedupe`, `transfer_type`, `compare` with `drop_matches`/`keep_matches`, `export`.

---

## 3. `load` object (optional)

```json
"load": { "limit": 2000, "columns": { "flat": ["reel_no","assignor_names","assignee_names"] } }
```

- `limit` (integer or `null`) — cap on the number of **assignment records** parsed. `null` = all.
  Use a small number (e.g. `1000`) for quick tests.
- `columns` (object, optional) — `table → [columns to load]`. Omit or `{}` to load all columns of
  all tables. Loading fewer columns/tables is faster (the parser skips building unused tables — most
  importantly the wide `flat` table when nothing needs it).

---

## 4. Enum reference (allowed values)

| Where | Field | Allowed values |
|---|---|---|
| filter clause | `op` | `contains`, `equals`, `not_equals` (null-safe exclusion), `starts_with`, `not_empty`, `is_empty`, `in_range` |
| filter | `combine` | `and`, `or` |
| normalize / compare / reference_match | `scorer` | `wratio` (default), `token_set`, `token_sort`, `partial`, `qratio`, `ratio`, `jaro_winkler` |
| classify / transfer_type | `method` | `rules` (default), `probablepeople` (optional ML; falls back to rules if not installed) |
| classify | `mode` | `all` (default), `any`, `first`, `majority` |
| classify output / transfer_type types | entity type | `company`, `individual`, `unknown` |
| compare | `method` | `exact` (default), `fuzzy` |
| compare | `action` | `flag` (default), `drop_matches`, `keep_matches` |
| reference_match | `mode` | `any` (default), `all` |
| reference_match | `action` | `flag` (default), `keep_matched`, `drop_matched` |
| derive | `op` | `year`, `month`, `split_first`, `upper`, `lower` |
| export | `fmt` | `parquet`, `csv`, `xlsx`, `json`, `feather` |

Threshold fields (`threshold`) are integers 0–100 (default `90`). `enabled` is a boolean (default
`true`); set `false` to keep a step in the template but skip it at run time.

---

## 4b. Algorithms & scorers — behaviour and when to use

> **Filters have no scorer.** A `filter` clause is an **exact** string predicate (contains / equals /
> starts_with / range / empty). Fuzzy **scorers** apply only to `normalize`, `compare` (`method:
> "fuzzy"`), and `reference_match`. Picking the right scorer/mode changes the *results*, so choose
> deliberately.

**Scorers** (the `scorer` field). All are rapidfuzz algorithms scored 0–100; `threshold` is the
minimum score to count as a match:

| Scorer | Behaviour | Use when |
|---|---|---|
| `wratio` (default) | Weighted blend, robust to length/word differences. | General cleanup; a safe default. |
| `token_set` | Ignores word **order** and **extra** tokens. | Reordered/expanded company names. **Caution:** may over-merge distinct entities — `ACME PHARMA INC`, `ACME PHARMACEUTICALS`, `ACME PHARMA INTERNATIONAL` can collapse into one (parent/sub/foreign arm). |
| `token_sort` | Ignores word order only (same tokens). | Word-order variants without extra words. |
| `partial` | Best matching substring. | One name is contained in the other. |
| `qratio` / `ratio` | Strict character similarity. | You want *tight* matching, few merges. |
| `jaro_winkler` | Rewards shared prefixes; good on short strings. | Tickers/abbreviations, short names. |

**Blocking (important).** `normalize` and gazetteer matching only compare a name against candidates
that share the **first 4 cleaned characters**. So `normalize` **cannot** merge an abbreviation with
its full name across different prefixes — e.g. `IBM` ↔ `INTERNATIONAL BUSINESS MACHINES`,
`HP` ↔ `HEWLETT PACKARD`. Only the reference gazetteer's `_disambiguated` / `assignee_id` output
resolves abbreviation-vs-full-name. Don't expect `normalize` to consolidate those. A prefix holding
more than ~10,000 names (the gazetteer's `THE …` bucket) is auto-**re-split onto a 6–8-char prefix**
so a crowded prefix never slows a match — this cap is shared by `normalize`, `reference_match`, and
the ledger pipeline, so full-`g_assignee_disambiguated.tsv` runs are safe at scale.

**`classify`** — `method: "rules"` (default; legal-suffix + org keywords, and the `LAST, FIRST`
person form) or `probablepeople` (optional ML; falls back to rules if not installed). `mode` combines
a multi-party `*_names` value: `all` (one agreed type across every party, else `unknown`), `any`
(company if any party is), `first`, `majority`.

**`compare`** — `method: "exact"` flags rows where the two columns are **identical** strings (fast;
ideal on the two `*_canonical` columns, or on two `*_assignee_id` columns). `method: "fuzzy"` flags
`scorer` ≥ `threshold`. Exact misses near-variants (`ACME INC` vs `ACME HOLDINGS INC`); use `fuzzy`
or an id-based compare to catch them (see §6b).

**`learn` (reproducibility).** `normalize` with `learn: true` (default) **writes to a shared,
persistent entity memory** that grows across every run and is merged back afterward — so canonical
forms become **path-dependent** (results depend on what you ran before). For **reproducible** runs,
set `learn: false` on every `normalize` step (match-only against a curated memory), or Clear the
memory between experiments. Treat a run as reproducible only under `learn: false`.

---

## 5. Step catalog (every `kind`, with fields + defaults)

Each step is an object with a `"kind"` discriminator. **The complete, closed set of valid kinds:**

```
filter · normalize · classify · compare · transfer_type · reference_match ·
fetch_cpc · cpc_match · dedupe · select · sort · derive · aggregate · export
```

Anything else (e.g. `resolve`, `ledger`, `join`, `dictionary`) is **not a template step** and will
fail to import — those capabilities exist as CLI subcommands, not steps (see §9). Note that
`fetch_cpc`/`cpc_match` are **purpose-built exact-join CPC steps** — do **not** try to attach CPC with
`reference_match` (a fuzzy *name* matcher; pointed at patent numbers it would silently corrupt them).
Fields marked
*(optional)* may be omitted. A `target`/`matched_target`/`id_target`/`out_table` left as `""` (or
omitted) **auto-derives** the name per §2a — prefer leaving it blank so names stay consistent.

### `filter` — keep rows matching clauses; optionally project columns and sort
```json
{ "kind": "filter", "table": "flat",
  "clauses": [ { "column": "conveyance_text", "op": "starts_with", "value": "assignment", "value2": "", "case_sensitive": false } ],
  "combine": "and",
  "columns": null,
  "sort": null }
```
- `clauses` — array of `{ "column", "op", "value", "value2", "case_sensitive" }`. `value2` is the
  upper bound for `in_range` only; `value`/`value2` are unused for `not_empty`/`is_empty`.
  `not_equals` is a **null-safe exclusion**: rows whose value differs are kept, and null/empty rows
  are kept too — the right shape for housekeeping like `purge_indicator not_equals "Y"`.
- `combine` *(optional)* — `and` (all clauses) or `or` (any). Default `and`.
- `columns` *(optional)* — array of columns to keep (projection), or `null` for all.
- `sort` *(optional)* — `["<column>", <ascending bool>]` or `null`. e.g. `["recorded_date", false]`.

### `normalize` — fuzzy-clean a name column to a canonical form
```json
{ "kind": "normalize", "table": "flat", "column": "assignor_names",
  "target": "", "threshold": 90, "separator": "", "learn": true, "scorer": "wratio",
  "emit_score": false, "review_threshold": 0, "emit_type": false }
```
- `column` — the name column (e.g. `assignor_names`, `assignee_names`, or `name` on assignors/assignees).
- `target` *(optional)* — output column; blank → `<column>_canonical`.
- `threshold` *(optional)* — fuzzy match cutoff (default 90). `separator` *(optional)* — blank
  auto-uses `"; "` for `*_names` columns. `learn` *(optional)* — `true` grows the shared entity
  memory; `false` = match-only. `scorer` *(optional)* — see enum table.
- `emit_score` *(optional)* — adds `<target>_score` (weakest party confidence, 0–100).
  `review_threshold` *(optional, 0 = off)* — adds `<target>_review` flagging fuzzy accepts
  scoring below it (exact 100 / unmatched 0 never flag).
- `emit_type` *(optional)* — adds `<target>_type` from the entity memory's **stored** company/
  individual/unknown tags (tag entities once in *Settings ▸ Entity memory ▸ Tag all*, reuse them
  here — no re-classification per run). Untagged canonicals yield `""`; a multi-party value reports
  the single agreed type across its parts, else `unknown`. A later Filter on `<target>_type equals
  company` keeps only firm buyers/sellers.

### `classify` — label a name column company / individual / unknown
```json
{ "kind": "classify", "table": "flat", "column": "assignor_names",
  "target": "", "method": "rules", "mode": "all", "separator": "" }
```
- `target` blank → `<column>_type`. `mode` combines multi-party values. `method`/`mode` per enums.

### `compare` — compare two columns row-wise; flag or drop/keep matches
```json
{ "kind": "compare", "table": "flat", "left": "assignor_names_canonical", "right": "assignee_names_canonical",
  "target": "", "method": "exact", "scorer": "wratio", "threshold": 90, "action": "flag",
  "emit_score": false, "review_threshold": 0 }
```
- `left`/`right` — columns to compare (often the two `*_canonical` columns). `method` `exact`
  (fast) or `fuzzy` (uses `scorer`/`threshold`). `action` `flag` adds `<left>_matches_<right>`;
  `drop_matches`/`keep_matches` filter rows. Use to remove self-transfers (assignor == assignee).
- `emit_score`/`review_threshold` *(optional)* — add `<target>_score` / `<target>_review`
  (appended **before** drop/keep filtering, so surviving rows keep their score).

### `transfer_type` — keep only a chosen assignor→assignee pairing (preset)
```json
{ "kind": "transfer_type", "table": "flat",
  "assignor_column": "assignor_names", "assignee_column": "assignee_names",
  "assignor_type": "company", "assignee_type": "company", "method": "rules" }
```
- Keeps rows where the assignor classifies as `assignor_type` **and** assignee as `assignee_type`
  (each ∈ `company`/`individual`/`unknown`). Default `company → company` = firm-to-firm only.

### `reference_match` — match a name column against a disambiguated-assignee reference file
```json
{ "kind": "reference_match", "table": "flat", "column": "assignor_names",
  "reference_path": "reference/g_assignee_disambiguated.tsv",
  "name_column": "disambig_assignee_organization", "id_column": "assignee_id",
  "target": "", "matched_target": "", "id_target": "",
  "threshold": 90, "scorer": "wratio", "separator": "", "mode": "any", "delimiter": "", "action": "flag",
  "emit_score": false, "review_threshold": 0 }
```
- `reference_path` — path to a `.tsv`/`.csv`/`.parquet` on the machine that imports/runs the
  template (e.g. USPTO/PatentsView `g_assignee_disambiguated.tsv`, or a compact extract). **This
  path must exist at run time**; the template stores it verbatim.
- `name_column` — the organization-name column in the reference (default
  `disambig_assignee_organization`). `id_column` *(optional)* — an id column to capture (e.g.
  `assignee_id`); leave `""` to skip. `delimiter` *(optional)* — blank auto-detects (`.tsv`→tab,
  `.csv`→comma). Outputs `<column>_disambiguated`, `<column>_matched`, and (if `id_column`)
  `<column>_assignee_id`. `mode` `any`/`all`; `action` `flag`/`keep_matched`/`drop_matched`.
- `emit_score`/`review_threshold` *(optional)* — add `<column>_match_score` /
  `<column>_match_review`. Validation now also checks the reference file actually **has** the
  configured `name_column`/`id_column` (a compact parquet built without an id has only
  `organization`), warning before the run instead of failing mid-run.

### `fetch_cpc` — attach CPC codes to a patent-number column (exact grant join)
```json
{ "kind": "fetch_cpc", "table": "flat", "column": "doc_number", "kind_column": "doc_kind" }
```
- Adds `cpc_codes` (full CPC symbols), `cpc_subclasses` (4-char grain), and `cpc_lookup_status`
  (`na` non-grant / `found` / `not_found` / `uncached`). Routes to **grants only** via `kind_column`;
  patent numbers are normalized to the bare grant number before the join.
- The data **source, cache, and network posture come from the project CPC config** (edited in
  *Settings ▸ CPC data source*, saved to `cpc_config.json`), **not** from the step. **Offline by
  default**: uncached numbers are only fetched when the run enables the network (the batch dialog's
  *Allow network* checkbox). A low hit-rate is warned here and **aborts** in `cpc_match` — it means
  the patent-number/CPC-key formats are misaligned, not "no data".

### `attach_cpc_file` — attach CPC from an uploaded file (PatSeer/CSV/Parquet), offline
```json
{ "kind": "attach_cpc_file", "table": "flat", "column": "doc_number", "kind_column": "doc_kind",
  "source_path": "cpc/patseer_export.csv", "patent_column": "Publication Number",
  "code_column": "CPC", "separator": ";" }
```
- Same output columns as `fetch_cpc` (`cpc_codes` / `cpc_subclasses` / `cpc_lookup_status`) and the
  same grant-only exact join, but the codes come from `source_path` — **fully offline, no API, no
  cache**. `patent_column`/`code_column` name the columns in the file; `separator` splits a cell
  packing several CPC codes (typical PatSeer export), blank = one code per row (USPTO
  `g_cpc_current` bulk). `source_path` must exist at run time.

### `cpc_match` — rank buyers per portfolio patent by CPC overlap
```json
{ "kind": "cpc_match", "table": "flat", "portfolio_mode": "patent_list",
  "portfolio_path": "portfolio.txt", "buyer_column": "assignee_names_canonical",
  "number_column": "doc_number", "kind_column": "doc_kind", "date_column": "transaction_date",
  "out_table": "matched_buyers_by_portfolio_patent", "overall_table": "matched_buyers_overall" }
```
- Reads CPC already attached by a prior `fetch_cpc` step. `portfolio_mode` — `patent_list` (a file of
  grant numbers, one per line; each footprint resolved via the same source/cache) or `footprint_file`
  (a pre-built `patent,cpc` file; no network). Creates **two tables**: per-portfolio-patent ranked
  buyers (`out_table`) and a cross-portfolio buyer summary (`overall_table`).
- **All match knobs — grain (`subclass`/`main_group`/`full_symbol`), overlap metric
  (`shared_count`/`jaccard`/`rarity_weighted`), threshold, ranking weights, min in-domain patents,
  and hit-rate floor — come from the project CPC config**, so they stay consistent across templates.
  Aborts if the CPC hit-rate is below the floor.

### `derive` — add a computed column
```json
{ "kind": "derive", "table": "flat", "source": "transaction_date", "target": "", "op": "year" }
```
- `op`: `year` (chars 0–4 of a `YYYYMMDD` date), `month` (chars 5–6), `split_first` (first `"; "`
  part), `upper`, `lower`. `target` blank → `<source>_<op>`. Prefer `transaction_date` over
  `recorded_date` as the year/month source — it is the true-event axis (see §top note on dates).

### `dedupe` — drop duplicate rows (keep first)
```json
{ "kind": "dedupe", "table": "flat", "subset": ["reel_no","frame_no"] }
```
- `subset` *(optional)* — key columns; `null`/omit = dedupe on the whole row.

### `select` — keep and reorder columns
```json
{ "kind": "select", "table": "flat", "columns": ["reel_no","frame_no","assignor_names_canonical"] }
```

### `sort` — order a table by a column
```json
{ "kind": "sort", "table": "flat", "column": "recorded_date", "ascending": false }
```

### `aggregate` — group + count into a new summary table
```json
{ "kind": "aggregate", "table": "assignees", "group_by": ["name"], "count_distinct": null, "out_table": "" }
```
- Creates a new table (default name `<table>_by_<group_by joined by _>`) with the group columns +
  `count`, sorted by count descending. `count_distinct` *(optional)* adds a distinct-count column.
  **Export this new table by name** in a later `export` step.

### `export` — write tables to files (optionally choose/order/rename final columns)
```json
{ "kind": "export", "fmt": "parquet", "tables": ["flat"],
  "columns": { "flat": ["reel_no","assignor_names_canonical","assignor_names_type"] },
  "renames": { "flat": { "assignor_names_canonical": "assignor_clean" } } }
```
- `fmt` — see enum. `tables` *(optional)* — array of tables to write, or `null`/omit = every table.
- `columns` *(optional)* — `table → ordered list of columns to keep` (a table absent = all its
  columns). `renames` *(optional)* — `table → { source_column: output_name }`.
- Prefer `parquet`/`csv` for large outputs; `xlsx` is slow above ~100k rows.
- Outputs are written to `<output-folder>/<template-name>/run_<timestamp>/<source-stem>/<table>.<ext>` (each run also gets a `manifest.json` audit record and `run.log`) (the output
  folder and inputs are chosen in the app, **not** in the template).
- **Convert mode** (a per-run checkbox in the batch dialog, *not* a template field): outputs land
  directly in the chosen folder named `<source-stem>_<table>.<ext>`, with no run subfolder and no
  manifest/summary. The bundled **12 - Convert to Parquet** template (a single `export` step,
  `fmt:"parquet"`, no `tables` → all five) is the intended pairing for bulk XML/ZIP → Parquet.

---

## 6. Authoring rules & gotchas

- **Order matters.** A step can only reference columns that exist at its position: base columns
  (§2) plus columns added by *earlier enabled* steps (§2a). E.g. put `normalize`/`classify`
  **before** any `filter`/`export` that uses `*_canonical` / `*_type`.
- Put a **`normalize`/`classify` step before** a `compare` on canonical columns, and a
  `reference_match` before any filter on `*_matched`.
- Dates are strings: use `in_range` with `YYYYMMDD` bounds; don't use numeric comparisons.
- `reference_path` is machine-specific — only include `reference_match` steps if the user has that
  file; otherwise the step is skipped at run time (and flagged by validation).
- Always end with at least one `export` step, or the pipeline produces no files.
- Blank `target`/`out_table` = auto-derived name (recommended). Only set an explicit `target` if you
  need a specific output column name.
- The app validates on run and shows ⚠ for a step referencing a missing column/table/reference; use
  **Preview…** to dry-run on a sample first.

---

## 6b. Methodology, pitfalls & how to check (buyer/seller analytics)

Analytic pipelines over this data have recurring failure modes. Each row is **pitfall → fix in the
template → how to verify with the tool**.

> **Which buyer templates to use.** `templates/buyer_identification_templates.reviewed.json` is the
> **canonical** set — every fix below is already applied (id-based self-transfer, `learn:false`,
> housekeeping, `transaction_date` year, honest distinct counts). The other bundled files are kept
> for comparison/exploration: `buyer_identification_templates.json` (the pre-review originals),
> `examples.json` (general recipes), and the single-purpose `01_…`–`07_…` set. **Scale note:** the
> reviewed/original/examples templates point `reference_match` at the full multi-GB
> `reference/g_assignee_disambiguated.tsv`, while the `0N_…` set uses a compact
> `reference/reference.parquet` (build it once with **Build compact…**), plus
> `10_dropped_sellers_audit.json` — the audit companion to `01`: same firm-to-firm gate but it
> *flags* assignors and exports only the unmatched sellers with their best gazetteer score, so you
> can see exactly what `01`'s `keep_matched` gate excluded. **Confidence policy:** row-level output
> templates (`01`, `05`, `10`) emit `*_match_score`/`*_match_review` columns; leaderboard/bridge
> templates (`02`–`04`, `07`–`08`) aggregate afterwards, so score columns would be dropped and are
> deliberately not enabled there. Either is **safe at scale**
> now — the fuzzy blocking is capped/re-split (§4b) — but a template still re-scans its reference per
> input file. For repeated, cross-run-stable buyer/seller identity at bulk scale, prefer the **ledger
> CLI** (§9): it resolves once against a build-once dictionary. Rule of thumb: **templates = fast
> exploratory passes; ledger = the analysis of record.**

1. **Silent empty / garbage output — the most likely failure.** A wrong `reference_path`, a
   `name_column` that doesn't match the file, or a strict match gate can zero the table and export an
   empty file with no error. The classic trap: pointing at a **compact** `reference.parquet` (whose
   column is `organization`) while leaving `name_column` as `disambig_assignee_organization` (the
   **raw** TSV's column) — nothing matches and `keep_matched` drops everything.
   **Check:** run **Preview…** with a small `load.limit` (e.g. 2000) and watch the row count survive
   each step; the tool now prints a red **`⚠ … left '<table>' EMPTY`** warning at run time and flags
   `⚠ dropped all rows` in the Preview per-step summary. If a step zeroes the table, it's the
   path/column/gate, not your data.

2. **Conveyance filter — recall hole + blind spot.** The templates match `contains "ASSIGNOR'S
   INTEREST"` (with the apostrophe — the dominant USPTO wording `ASSIGNMENT OF ASSIGNOR'S INTEREST`,
   ~479k rows in a typical daily file). Earlier versions searched `ASSIGNORS INTEREST` *without* the
   apostrophe, which matched almost nothing and left `flat` empty — fixed across all bundled
   templates. It still misses rarer variants (older DTD text) and excludes nunc-pro-tunc / corrective
   assignments (some are the only record of a real transfer), and does **not** separate
   inventor→employer from firm→firm — both carry that text.
   **Fix/Check:** run an `aggregate` on `conveyance_text` (group_by it) to see the real vocabulary,
   then build an **OR** filter of the strings you actually see. Separate inventors using the **seller
   gate / classify**, not the conveyance text.

3. **Seller-gate bias (foreign & multi-party).** `reference_match … mode:"all"` drops a whole deal if
   any one assignor is awkwardly formatted; the gazetteer is US-centric, so transliterated foreign
   sellers (`KABUSHIKI KAISHA` vs `K.K.` vs the English name) fail to match and vanish — a systematic
   undercount, not random noise. And validating **assignors** with an **assignee** gazetteer assumes
   every seller was also a grant assignee (shaky for pure-sell vehicles).
   **Fix/Check:** prefer `action:"flag"` + `mode:"any"` and treat **unmatched as "unconfirmed,"** not
   "not a company"; measure the drop rate at `all` vs `any` via Preview before trusting a full run.

4. **Self-transfer removal only catches the easy half.** `compare … method:"exact"` on the two
   `*_canonical` columns drops only identical strings — intra-group reorganizations
   (`ACME INC` → `ACME HOLDINGS INC`, parent→subsidiary) survive and pollute the buyer set.
   **Fix:** you already have a better key — `reference_match` emits `<column>_assignee_id` on **both**
   sides. Add a `compare` on `assignor_names_assignee_id` vs `assignee_names_assignee_id`
   (`method:"exact"`, `action:"drop_matches"`) — same gazetteer entity on both sides is a far cleaner
   self-transfer test — and keep a `method:"fuzzy"` canonical compare as a fallback for off-gazetteer
   rows (empty ids).

5. **Normalization traps.** (a) `learn:true` = non-reproducible (see §4b) — use `learn:false`.
   (b) `token_set` may merge distinct legal entities — decide whether you want the *economic* or
   *legal* buyer. (c) blocking means `normalize` won't merge `IBM`/`INTERNATIONAL BUSINESS MACHINES` —
   lean on the gazetteer's `_disambiguated`/`assignee_id` for abbreviation↔full-name.

6. **`count_distinct doc_number` overstates "patents."** `doc_number` holds application, publication,
   **or** grant numbers depending on `doc_kind` (different number spaces), so the same invention can
   appear twice and inflate a leaderboard.
   **Fix:** `filter` to a single `doc_kind` (e.g. grants) **before** aggregating, and rename the
   output metric honestly to `distinct_document_ids`. (A `reel_no`+`frame_no` "deals" count is a
   reasonable proxy but one M&A event can span several recordings.)

7. **Time axis biased late / handled inconsistently.** Use **`transaction_date`** (via `derive`
   `op:"year"`) instead of `recorded_date`, which lags reality — `transaction_date` is the latest
   `execution_date`, or `recorded_date` when none was filed, so it is always populated (unlike a bare
   `execution_date`, which is empty for filings without signer dates). Apply the **same** housekeeping
   to every template (don't filter dates in some but not others).

8. **Housekeeping.** Exclude purged records with `purge_indicator not_equals "Y"` (the field holds a literal
   `N`/`Y` flag — an `is_empty` test drops **every** row, the exact silent-zeroing trap of #1), and drop empty/partial dates (`recorded_date not_empty`). The "company OR
   unknown" buyer rule keeps brand/shell buyers but makes the off-gazetteer file the noisiest quadrant
   — review the top of that list by frequency and tune the enrichment `threshold` if firms leak in.

---

## 7. Full example file (ready to import)

```json
[
  {
    "name": "Firm-to-firm, enriched",
    "load": { "limit": null },
    "steps": [
      { "kind": "filter", "table": "flat",
        "clauses": [ { "column": "conveyance_text", "op": "starts_with", "value": "assignment" },
                     { "column": "transaction_date", "op": "in_range", "value": "20060101", "value2": "20261231" } ],
        "combine": "and" },
      { "kind": "normalize", "table": "flat", "column": "assignor_names" },
      { "kind": "normalize", "table": "flat", "column": "assignee_names" },
      { "kind": "classify", "table": "flat", "column": "assignor_names" },
      { "kind": "classify", "table": "flat", "column": "assignee_names" },
      { "kind": "transfer_type", "table": "flat", "assignor_type": "company", "assignee_type": "company" },
      { "kind": "derive", "table": "flat", "source": "transaction_date", "op": "year" },
      { "kind": "export", "fmt": "parquet", "tables": ["flat"],
        "columns": { "flat": ["reel_no","frame_no","transaction_date_year",
                               "assignor_names_canonical","assignee_names_canonical"] },
        "renames": { "flat": { "assignor_names_canonical": "assignor_clean",
                               "assignee_names_canonical": "assignee_clean",
                               "transaction_date_year": "year" } } }
    ]
  },
  {
    "name": "Top assignees by patent count",
    "load": { "limit": null },
    "steps": [
      { "kind": "normalize", "table": "assignees", "column": "name" },
      { "kind": "aggregate", "table": "assignees", "group_by": ["name_canonical"] },
      { "kind": "export", "fmt": "csv", "tables": ["assignees_by_name_canonical"] }
    ]
  },
  {
    "name": "Remove self-transfers",
    "load": { "limit": null },
    "steps": [
      { "kind": "normalize", "table": "flat", "column": "assignor_names" },
      { "kind": "normalize", "table": "flat", "column": "assignee_names" },
      { "kind": "compare", "table": "flat",
        "left": "assignor_names_canonical", "right": "assignee_names_canonical",
        "method": "exact", "action": "drop_matches" },
      { "kind": "export", "fmt": "parquet", "tables": ["flat"] }
    ]
  }
]
```

---

## 8. Prompt to give an assistant

> Using the spec in `templateInfo.md`, generate a JSON file (a top-level array) of batch templates
> for the USPTO patent-assignment tool. For each template I describe, produce a template object with
> `name`, an optional `load`, and an ordered `steps` array. Only use the listed table names, column
> names, and enum values, and **only the 14 step kinds listed in §5** — the buyer-identification
> pipeline (`ingest`/`build-dictionary`/`resolve`/`ledger`/`report`) is a CLI, not a set of step
> kinds, so never invent steps like `resolve` or `join`. For CPC work use the dedicated `fetch_cpc`
> and `cpc_match` steps (never `reference_match` on patent numbers). Respect step ordering so later
> steps only
> reference columns that exist by then (base columns + columns earlier steps add). End each pipeline
> with an `export` step. Output only valid JSON that will import via **Settings ▸ Batch processing ▸
> Import…**.

---

## 9. Capabilities beyond templates (the buyer-identification CLI pipeline)

Templates cover interactive exploration and per-file exports. A **separate CLI pipeline** (not
template steps — see GUIDE.md §3b for full usage) does full **entity resolution + transaction
analysis** with persisted, versioned outputs:

```
uspto-assign ingest <xml|zip|dataset-dir> --out data/raw       # land raw Parquet, natural grain
uspto-assign build-dictionary --patentsview <tsv> --out dictionary   # build-once local artifact
uspto-assign ledger --raw data/raw --dict dictionary --out data/ledger
uspto-assign report --ledger data/ledger --by patents|deals [--cpc-file …] [--cpc-mode sampled|full]
```

What it adds over templates: a curated **conveyance taxonomy** (9 types, regex-based — sturdier
than substring filters); **transaction reconstruction** (true execution-based dates with a
`date_source` flag); an **entity-resolution cascade** (O(1) exact lookup incl. legal-suffix-stripped
keys → person detector → capped blocked fuzzy → **stable provisional ids** for off-gazetteer
entities); a **firm-to-firm predicate on entities** (not strings — inventors and intra-group
reorgs drop out); and three linked Parquet outputs joined on `entity_id`: `transaction_ledger`,
`buyers` (with `resolution_source`, confidence, `is_off_gazetteer`), and the **CPC-ready**
`buyer_property_bridge` (`doc_type`, `patent_id_normalized` in PatentsView convention). Running
`report --cpc-file <cpc table>` joins CPC and **writes the codes onto the bridge** —
`cpc_codes` (full symbols per patent) and `cpc_subclasses` (4-char, `H04L …`) — plus
`cpc_lookup_status` and a `cpc_hit_rate` reconciliation metric that fails loudly on format mismatch.

**When to author a template vs point at the CLI:** name cleanup, filtering, per-file enrichment and
exports → template. Buyer/seller identity, deal ledgers, cross-run-stable entity ids, CPC feeds →
CLI pipeline. They compose: templates for exploration, the pipeline for the analysis of record.

---

## 10. Machine learning & external models — what exists, what plugs in

**Built into the tool (no downloads, works offline):**

| Capability | Where | Notes |
|---|---|---|
| Rule-based entity-type classifier | `classify` step / `transfer_type` (`method: "rules"`) | legal-suffix + org-keyword detection, `LAST, FIRST` person form; deterministic and fast |
| **probablepeople** (statistical CRF model) | `method: "probablepeople"` on `classify`/`transfer_type` | the only bundled *learned* model; optional install (`pip install -e ".[ml]"`); parses person-vs-corporation; falls back to rules if absent |
| rapidfuzz similarity algorithms | `scorer` on `normalize`/`compare`/`reference_match` | 7 algorithms (§4b) — string similarity, not ML |
| Learnable entity memory | `normalize` with `learn: true` | grows alias→canonical mappings from data (path-dependent — see §4b) |

**NOT built in — do not reference these as step options:** there is **no PatentBERT, no
transformer/embedding model, no neural CPC classifier, and no network-accessed model** inside the
tool. A template that names them will not import.

**How external models plug in anyway** — the tool is designed as the *data side* of an ML loop;
any model's output re-enters through four file-based integration points:

1. **`reference_match` step** — point `reference_path` at any model-produced gazetteer
   (`.tsv/.csv/.parquet` with a name column + optional id column). E.g. an embedding-based
   deduplication of company names exported as `organization,entity_id` becomes an authoritative
   matcher inside templates.
2. **Entity-memory Import** (Settings ▸ Entity memory) — a curated `alias,canonical` CSV produced
   by any model becomes the exact-match layer for `normalize` (run match-only with `learn: false`).
3. **`build-dictionary --extra PATH:NAME_COL:ID_COL:SOURCE`** (CLI) — merge model-produced entity
   lists (GLEIF extracts, SEC tickers, your own clustered names) into the resolution dictionary.
4. **`report --cpc-file <file>`** (CLI) — the intended **PatentBERT-style hook**: the
   `buyer_property_bridge` carries `patent_id_normalized` + `doc_type` precisely so you can run an
   external CPC classifier (PatentsView `g_cpc_current`, or your own model over titles/claims) and
   feed the result back; the tool computes `cpc_hit_rate` and flags format misalignment loudly.
   Bridge `invention_title` + grant routing (`doc_type = grant`) are there to drive downstream
   title/abstract/claim models — run those outside the tool, on the exported Parquet.
