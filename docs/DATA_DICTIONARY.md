# Data dictionary

Every column in every file this tool **reads** or **writes** — the source XML, the reference and
CPC inputs, the four normalized tables plus the wide `flat` view, every column a pipeline step
adds, the CPC-match outputs, and the run-audit files.

## Conventions
- **Every data value is a nullable string.** USPTO numbers carry significant **leading zeros**
  (`reel_no` `012345`) and dates may be partial, so values are preserved verbatim, never coerced to
  int/date. (`src/uspto_assignments/model.py`.)
- **Dates** are `YYYYMMDD`, sometimes partial (`YYYY0000` when only a year is known).
- **Keys** — an assignment (recordation) is identified by `reel_no` + `frame_no`; every assignor,
  assignee, and property row links back to its assignment by that pair.
- **List columns** (e.g. `cpc_codes`) hold multiple values; when written to CSV/Excel/JSON they are
  joined with `; `.
- Tables written by the pipeline are all-string; column **order** matches the schema below.

---

## 1. Input files

### 1a. USPTO patent-assignment XML / ZIP (the source)
The bulk file (e.g. `ad20260709.zip`, read straight from the archive) is deeply nested: each
`<patent-assignment>` carries one **assignment header** plus repeating **assignors**, **assignees**,
and **patent properties** (each property carrying one or more `document-id` blocks). The parser
normalizes that into the four tables in §2 — so the source fields *are* those columns. No other
input schema needs to be memorized; §2 is the extracted result.

### 1b. Reference gazetteer (for `reference_match`)
Two accepted shapes, both keyed on organization name:

**`reference/reference.parquet`** — the compact gazetteer this tool builds
(`uspto-assign build-reference`). Columns:

| Column | Definition |
|---|---|
| `organization` | Distinct canonical organization (assignee) name. |
| `assignee_id` | Stable PatentsView disambiguated-assignee id for that organization (may be blank if built name-only). |

**`reference/g_assignee_disambiguated.tsv`** — the raw PatentsView bulk file (usable directly).
Relevant columns: `assignee_id`, `disambig_assignee_organization` (org name),
`disambig_assignee_individual_name_first` / `_last` (person names), `assignee_type`, plus
`patent_id`, `assignee_sequence`, `location_id`. The `reference_match` step reads the org column
(`name_column`) and optionally the id column (`id_column`).

### 1c. CPC file (for `attach_cpc_file`)
A CSV/TSV/Parquet export (e.g. from PatSeer) with **at least** a patent-number column and a CPC
column; column names are configurable on the step (defaults shown):

| Step field | Default | Definition |
|---|---|---|
| `patent_column` | `Publication Number` | The grant/publication number to join on. |
| `code_column` | `CPC` | The CPC symbol(s); if several are packed in one cell, set `separator` (default `;`). |

### 1d. Portfolio inputs (for `cpc_match`)
- **`patent_list`** (`portfolio.txt`) — one grant number per line; each patent's CPC footprint is
  resolved via the CPC source/cache.
- **`footprint_file`** (`portfolio_footprint.csv` / `.parquet`) — a pre-built footprint, **≥2
  columns**: column 1 = patent number, column 2 = CPC code (one code per row). No network used.

### 1e. Processed data-file inputs
A previously-exported `.parquet` / `.arrow` / `.feather` / `.csv` (single file or dataset folder)
can be fed straight back into batch. A single file loads as the `flat` table (see §2e); its columns
are whatever a prior run wrote — i.e. §2 + §3.

---

## 2. Core tables (base columns produced by parsing)

### 2a. `assignments` — one row per recordation (reel/frame)
| Column | Definition |
|---|---|
| `reel_no` | Recordation reel number (leading zeros significant). Part of the key. |
| `frame_no` | Recordation frame number. Part of the key. |
| `last_update_date` | Date the assignment record was last updated. |
| `recorded_date` | Date the assignment was recorded at the USPTO. |
| `purge_indicator` | `Y` when the record is purged/withdrawn (templates exclude these). |
| `page_count` | Number of pages in the recorded document. |
| `conveyance_text` | Free-text conveyance description (e.g. `ASSIGNMENT OF ASSIGNOR'S INTEREST`, `MERGER`) — the firm-to-firm gate keys on this. |
| `correspondent_name` | Filing correspondent's name. |
| `correspondent_address_1`…`_4` | Correspondent address lines 1–4. |

### 2b. `assignors` — one row per assignor (the seller/transferor side)
| Column | Definition |
|---|---|
| `reel_no`, `frame_no` | Link to the parent assignment. |
| `name` | Assignor (transferring party) name. |
| `execution_date` | Date this assignor signed/executed the assignment. |
| `date_acknowledged` | Date the execution was acknowledged/notarized. |

### 2c. `assignees` — one row per assignee (the buyer/transferee side)
| Column | Definition |
|---|---|
| `reel_no`, `frame_no` | Link to the parent assignment. |
| `name` | Assignee (receiving party) name. |
| `address_1`, `address_2` | Assignee street address lines. |
| `city`, `state`, `country_name`, `postcode` | Assignee location. |

### 2d. `properties` — one row per (patent property × document-id)
| Column | Definition |
|---|---|
| `reel_no`, `frame_no` | Link to the parent assignment. |
| `invention_title` | Title of the patent/application. |
| `doc_country` | Document country code (e.g. `US`). |
| `doc_number` | Patent/application/publication number. **The number space depends on `doc_kind`** — application, publication, and grant numbers differ, so a distinct-count without a `doc_kind` gate mixes them. |
| `doc_kind` | Document kind code (`A1`, `B1`, `B2`, …). **`B*` = granted patents**; templates counting patents gate on `doc_kind starts_with "B"`. |
| `doc_name` | Document name/type label. |
| `doc_date` | The document's date. |

### 2e. `flat` — the wide denormalized view (one row per property × document-id)
`flat` joins the **assignment header** (all of §2a) + **party rollups** + **property fields**
(§2d) into one row per property-document, so it is the table almost every template operates on.
Beyond the §2a and §2d columns, `flat` adds:

| Column | Definition |
|---|---|
| `assignor_names` | All assignor names for the assignment, joined with `; `. |
| `assignee_names` | All assignee names for the assignment, joined with `; `. |
| `assignor_count` | Number of assignors on the assignment. |
| `assignee_count` | Number of assignees on the assignment. |
| `execution_date` | Rollup of assignor execution dates — the **latest** signer date. |
| `date_acknowledged` | Rollup of assignor acknowledgement dates. |
| `transaction_date` | **Effective transfer date for time-axis work** — the latest `execution_date`, or `recorded_date` when no signer date was filed. Prefer this over `recorded_date`. |
| `date_source` | Which date `transaction_date` came from: `execution` or `recorded`. |

---

## 3. Columns added by pipeline steps
Steps append columns to the table they run on (`<col>` = the input column, e.g. `assignee_names`).

| Step | Column(s) added | Definition |
|---|---|---|
| **normalize** | `<col>_canonical` | Fuzzy-canonicalized (de-duplicated) form of the name. |
| | `<col>_canonical_score` | Match confidence (0–100) — only when *emit score* is on. |
| | `<col>_canonical_review` | `true` when the score is below the review threshold (needs a human look) — only when a review threshold is set. |
| | `<col>_canonical_type` | Entity type of the canonical (company/individual/unknown) — only when *emit type* is on. |
| **classify** | `<col>_type` | Entity type: `company`, `individual`, or `unknown`. |
| **derive** | `<source>_<op>` | Computed field, e.g. `transaction_date_year` = the `YYYY` of `transaction_date` (ops: `year`, `month`, `split_first`, `upper`, `lower`). |
| **compare** | `<left>_matches_<right>` | `true`/`false` — whether the two columns matched (only when action = *flag*). |
| | `<target>_score` / `<target>_review` | Fuzzy score / review flag, when enabled. |
| **reference_match** | `<col>_disambiguated` | The gazetteer's canonical org name for the best match (blank if none). |
| | `<col>_matched` | `true`/`false` — whether the name resolved to a gazetteer org. |
| | `<col>_assignee_id` | The gazetteer `assignee_id` of the match (when an id column is configured). |
| | `<col>_match_score` | Match confidence (0–100) — when *emit score* is on. |
| | `<col>_match_review` | `true` when the score is below the review threshold. |
| **fetch_cpc / attach_cpc_file** | `cpc_codes` | List of full CPC symbols for the patent (e.g. `H04L9/32`). |
| | `cpc_subclasses` | List of 4-char CPC subclass grains (e.g. `H04L`). |
| | `cpc_lookup_status` | `na` (non-grant, skipped), `found`, `not_found`, or `uncached` (offline & not in cache). |
| **aggregate** | *(group-by columns)* | Carried through from the input, one row per group. |
| | `count` | Number of rows in the group. |
| | `<col>_distinct` | Number of distinct values of `count_distinct` in the group (e.g. `doc_number_distinct`). |

---

## 4. CPC-match output tables (`cpc_match`)
Match a sales-package portfolio against buyer CPC footprints. Produces two tables, plus a third
when *emit class matches* is on.

### 4a. `matched_buyers_by_portfolio_patent` — ranked buyers **per** portfolio patent
| Column | Definition |
|---|---|
| `portfolio_patent` | The portfolio (sales-package) grant number being matched. |
| `buyer` | Candidate buyer (canonical name, or a `prov-…` provisional id for an unresolved entity). |
| `overlap_strength` | Summed CPC-overlap score across the buyer's matching patents (4 dp). |
| `in_domain_patents` | Count of the buyer's patents sharing ≥1 CPC class with this portfolio patent. |
| `last_acquisition_date` | Most recent acquisition year among the buyer's matching patents. |
| `shared_codes` | Sorted list of CPC classes shared between the portfolio patent and the buyer. |
| `is_off_gazetteer` | `true` if the buyer is an unresolved/provisional entity, else `false`. |
| `rank_score` | Weighted rank score = w·overlap + w·volume + w·recency (from the CPC settings). |
| `rank` | 1-based rank of this buyer for this portfolio patent. |

### 4b. `matched_buyers_overall` — buyers rolled up **across** the whole portfolio
| Column | Definition |
|---|---|
| `buyer` | Candidate buyer. |
| `portfolio_patents_matched` | Number of distinct portfolio patents this buyer matched. |
| `total_overlap_strength` | Summed overlap strength across all portfolio patents. |
| `in_domain_patents` | Total in-domain buyer patents. |
| `last_acquisition_date` | Most recent acquisition year across all matches. |
| `is_off_gazetteer` | `true` for an unresolved/provisional buyer, else `false`. |

### 4c. `matched_cpc_classes` — per-class evidence (one row per portfolio patent × buyer patent × shared class)
| Column | Definition |
|---|---|
| `portfolio_patent` | The portfolio grant number. |
| `buyer` | The candidate buyer. |
| `buyer_patent` | The buyer's own patent providing the evidence. |
| `cpc_class` | The CPC class shared between the two. |
| `year` | Acquisition year of the buyer patent. |
| `is_off_gazetteer` | `true` for an unresolved/provisional buyer, else `false`. |

---

## 5. Run-audit & index files
Written by a normal (non-convert) batch run into the run folder, plus the cross-run/convert ledgers.

### `manifest.json` (per run)
Keys: `schema` (format version), `template` (`name` + a `steps` list of `{index, kind, enabled,
summary}`), `timestamp`, `generated` (ISO time), `duration_seconds`, `workers`, `cancelled`,
`strict`, `warnings` (list), `inputs` (source paths), `summary` (`{succeeded, failed}`), and
`files` — one entry per source: `source`, `ok`, `error`, `elapsed`, `rows` (per-table counts),
`outputs` (each `{path, table, format, rows}`), `steps` (per-step stats), `step_outputs`.

### `runs_index.csv` (cross-run ledger, appended in the output root)
| Column | Definition |
|---|---|
| `timestamp` | Run stamp. |
| `template` | Template name. |
| `inputs` | Number of input files in the run. |
| `succeeded` / `failed` | Per-run success/failure counts. |
| `cancelled` | Whether the run was cancelled early. |
| `run_dir` | The run folder (relative to the output root). |

### `_convert_index.csv` (convert-mode breadcrumb, appended in the output folder)
| Column | Definition |
|---|---|
| `timestamp` | Run stamp. |
| `source` | The input file converted. |
| `status` | `ok` or `failed`. |
| `outputs` | Output filenames written for this source (`; `-joined). |
| `rows` | Per-table row counts (e.g. `flat=1234`). |
| `error` | Error message if the file failed, else blank. |

### `summary.xlsx` / `run.log`
`summary.xlsx` is a spreadsheet view of the same per-file / per-output rows the manifest records;
`run.log` is a plain-text line per source (`<source>: OK/FAILED  <elapsed>s  rows=… outputs=…`).
Per-step review files (`steps/NN_<table>.<ext>`) are the working table after each step, in the
format chosen for the trace.
