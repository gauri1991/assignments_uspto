# Batch template audit — check notes

A step-by-step correctness audit of every bundled batch-pipeline template in `templates/`,
cross-checked against the engine's real contract (`src/uspto_assignments/batch.py`) and the
documented intent (`templateInfo.md`, `GUIDE.md`, `help_content.py`). Findings A–C have since been
**fixed**; this document records the audit and the fixes.

## How it was verified (not just read — executed)
1. Extracted the ground-truth step-engine contract from `batch.py` — decode/validate
   (`_decode_step`), execute (`_apply_step`), and pre-run warnings (`validate_template`).
2. Dumped every concrete template config from `templates/*.json` (17 files, ~34 templates).
3. Extracted documented intent + gotchas from `templateInfo.md`, `GUIDE.md §10/§11`, `help_content.py`
   and the validation tests.
4. **Ran the engine's own decoder + validator against all 17 files** (read-only). Verified the
   data-layer facts directly:
   - `scorer=token_set` is a registered scorer (`normalize.py`).
   - `reference/reference.parquet` really has columns `organization` + `assignee_id` — what the
     numbered templates pass.
   - `reference/g_assignee_disambiguated.tsv` has `disambig_assignee_organization` + `assignee_id`.
   - The `flat` base schema (`model.py FLAT_COLUMNS`) carries every base column the filters reference.

## Headline result
**All templates are structurally and configurationally valid.** Every one decodes without a
`TemplateFormatError` (enums / required fields / shapes / thresholds / scorers all correct) and
passes `validate_template` apart from the expected, by-design warnings below. There are **no broken
step configs**.

## Validator verdict (post-fix)
| Template(s) | Verdict |
|---|---|
| 01, 02, 03, **04**, 05, 06, 07, 08, 10, 12 | OK (0 warnings) |
| 09 | 2 softened "needs a processed input" notes + `portfolio.txt` not found (placeholder) |
| 11 | `cpc/patseer_export.csv` not found (placeholder) |
| 13 | `cpc/patseer_export.csv` not found (placeholder) |
| 14 | 2 softened "needs a processed input" notes + `cpc/portfolio_footprint.csv` not found (placeholder) |
| `buyer_identification_templates.json` (4), `.reviewed.json` (5), `examples.json` (7) | OK (0 warnings) |

Every remaining warning is either a **placeholder data path** the user supplies, or the softened
**"needs a processed input"** note for `cpc_match` — neither is a config defect.

## Findings

### A — Template 04 counted documents across all doc-kinds (no grant gate)  · FIXED
`04_buyers_matched_entity_accurate.json` aggregated `count_distinct=doc_number` with no
`doc_kind` filter, unlike template 03. Because `doc_number` mixes application/publication/grant
number spaces (the "counting grains" gotcha, `templateInfo.md`), an un-gated distinct count can
over-count. **Fix:** added a `doc_kind starts_with "B"` clause to step 2, matching template 03, so
`distinct_document_ids` now counts granted documents only.

### B — Template 11 had a weak self-transfer gate  · FIXED
`11_attach_cpc_from_file.json` step 4 compared **raw** `assignor_names` vs `assignee_names` (exact)
— the weakest self-transfer removal (misses "ACME INC" vs "ACME INC."). **Fix:** replaced it with
the pattern template 13 uses — two `normalize` steps (`learn:false`, `scorer:token_set`) then a
`compare` on `assignor_names_canonical` vs `assignee_names_canonical`; added
`assignee_names_canonical` to the export columns for parity with 13. (The `recorded_date`
housekeeping clause is intentionally left off — 11 and 13 are a consistent grant-gated pair that
both omit it.)

### C — Reference-file convention was split  · FIXED
The docs-designated **canonical** `buyer_identification_templates.reviewed.json` pointed at the raw
1.1 GB `g_assignee_disambiguated.tsv`, while the numbered 01–14 use the compact 27 MB
`reference.parquet` (both have valid columns; the TSV is ~40× larger and slower). **Fix:** repointed
every `reference_match` step in the reviewed set to `reference/reference.parquet` with
`name_column: organization` (and updated the curated help text). `examples.json` (teaching recipes)
and `buyer_identification_templates.json` (pre-review baseline, kept for comparison) are left on the
TSV **intentionally**.

### D — Placeholder data paths in 09 / 11 / 13 / 14  · INFO (by design)
`portfolio.txt` (09), `cpc/patseer_export.csv` (11, 13), `cpc/portfolio_footprint.csv` (14) do not
exist in the repo — they are placeholders the user replaces with their own portfolio / PatSeer /
footprint files. The validator warns; the step then skips (attach) or aborts (cpc_match patent_list
mode, `HitRateError`).

### E — Templates 09 & 14 require a *processed* input, not raw XML/ZIP  · INFO (by design)
`cpc_match` reads `cpc_codes` and `assignee_names_canonical`, which come from a prior run. 09 is
designed to consume the output of 07/08; 14 the output of 13. The validator softens the column
warning for exactly this chain. **The batch input layer now accepts a processed Parquet/Arrow/
Feather/CSV file (or a dataset folder) directly** (see `GUIDE.md` §Batch), so re-feeding a processed
dataset is a first-class flow.

## Non-issues explicitly checked and cleared
- `scorer=token_set` — valid alias (not `token_set_ratio`).
- Numbered templates' `name_column=organization` / `id_column=assignee_id` match `reference.parquet`.
- The conveyance OR filter carrying both `ASSIGNORS INTEREST` and `ASSIGNOR'S INTEREST` — correct;
  the apostrophe clause does the work, the other is a harmless extra OR arm (this is the *fix* for
  the old empty-`flat` gotcha, not a regression).
- All `in_range` filters carry `value2`.
- Auto-derived table names line up with downstream `sort`/`export` references in `examples.json`.
- Every pipeline ends in an `export` step; aggregate-created tables get their own export.

## Reproduce
Decode + validate every bundled template against the fresh-parse schema (read-only, writes nothing):

```python
from pathlib import Path
from uspto_assignments import batch as B, model as M
base = {t: list(M.columns_for(t)) for t in ("assignments", "assignors", "assignees", "properties", "flat")}
for path in sorted(Path("templates").glob("*.json")):
    for tpl in B.load_templates(path):
        warnings = B.validate_template(tpl.load, tpl.steps, base=base)
        print(("OK   " if not warnings else f"{len(warnings)}warn"), tpl.name)
        for w in warnings:
            print("      -", w)
```

Regenerate the per-step listing in the appendix below with the engine's own step describer:

```python
from pathlib import Path
from uspto_assignments import batch as B
for path in sorted(Path("templates").glob("*.json")):
    for tpl in B.load_templates(path):
        print(f"{tpl.name}")
        for i, s in enumerate(tpl.steps, 1):
            print(f"  {i:2d}. {B.describe_step(s)}")
```

---

# Appendix — per-template step-by-step

Step labels below are the engine's own (`describe_step`), reflecting the **current** (post-fix)
templates. To avoid repeating prose, the recurring building blocks are defined once here; each
template then notes only its goal, its output, and anything specific.

## Shared building blocks
- **Conveyance gate (OR)** — `conveyance_text contains "ASSIGNORS INTEREST"` **or**
  `"ASSIGNOR'S INTEREST"`. Keeps "assignment of assignor's interest" deals (the firm-to-firm sale
  wording, ~479k rows/daily file). The apostrophe clause does the work; the no-apostrophe arm is a
  harmless catch-all. *Correct — this is the fix for the old empty-`flat` bug.*
- **Housekeeping gate (AND)** — `assignee_names not_empty` + `purge_indicator not_equals "Y"` +
  `recorded_date not_empty`, and **+ `doc_kind starts_with "B"`** when the template counts granted
  patents. Drops empty/purged/undated rows; the `B` clause restricts to grants. *`not_equals` is
  null-safe; never uses `is_empty`, which would drop every row.*
- **Seller gazetteer gate** — `reference_match` on `assignor_names` vs `reference.parquet`.
  `keep_matched · mode=all` = strict (every assignor must resolve to a known org, or the deal is
  dropped); `flag · mode=any` = soft (keeps all rows, just marks matches). Adds
  `assignor_names_disambiguated/_matched/_assignee_id` (+ `_match_score/_match_review` when scored).
- **Buyer classify + company/unknown filter** — `classify assignee_names → assignee_names_type`,
  then `filter` keeps `type == company OR unknown`. Drops individual buyers; keeps brand/shell
  buyers (the intended "company OR unknown" rule).
- **Buyer gazetteer flag** — `reference_match` on `assignee_names` vs `reference.parquet`,
  `flag · mode=any`. Marks whether the buyer is a known org without dropping rows.
- **Self-transfer removal** — `compare … drop_matches` to delete intra-entity reassignments. By
  **id** (`assignor_names_assignee_id` vs `assignee_names_assignee_id`, exact) is strongest;
  by **canonical name** (exact, or fuzzy ≥92 in the reviewed set) is the fallback when no id exists.
  *Two empty values never match, so id-less rows aren't nuked.*
- **Normalize** — adds `<col>_canonical`. `· match-only` means `learn:false` (reproducible: does not
  grow the shared entity memory).
- **Derive year** — `transaction_date_year = year(transaction_date)`. *Correct time axis —
  `transaction_date` = latest execution date else recorded date, not the lagging `recorded_date`.*

---

## 01 — Firm-to-firm transactions (clean, enriched)
Row-level clean firm-to-firm deals, seller **and** buyer resolved against the gazetteer. → `flat.parquet`.
1. Conveyance gate (OR).
2. Housekeeping gate (AND).
3. Seller gazetteer gate — `keep_matched · mode=all`, scored, `review<95`.
4. Classify buyer → `assignee_names_type`.
5. Company/unknown buyer filter.
6. Buyer gazetteer flag — scored, `review<95`.
7. Self-transfer removal **by id** (exact).
8–9. Normalize assignor + assignee → `_canonical`.
10. Self-transfer removal **by canonical** (exact) — second net for id-less rows.
11. Derive year.
12. Export `flat` (parquet), renamed to seller/buyer `_clean/_id/_match_*` columns.

## 02 — Buyer leaderboard, deals closed
Counts **distinct deals** per buyer. → `buyers_by_deals.csv`.
1–2. Conveyance + housekeeping gates.
3. Seller gazetteer gate (`keep_matched · mode=all`).
4–5. Classify buyer + company/unknown filter.
6. **Dedupe on `reel_no, frame_no`** — one row per deal *before* counting (the deal grain).
7–8. Normalize assignor + assignee.
9. Self-transfer removal (canonical, exact).
10. Aggregate by `assignee_names_canonical` → `buyers_by_deals` (count = deals).
11. Export.

## 03 — Buyer leaderboard, patents (documents) acquired
Counts **distinct granted documents** per buyer. → `buyers_by_documents.csv`.
1. Conveyance gate. 2. Housekeeping gate **incl. `doc_kind starts_with "B"`** (grants only — the
counting-grain rule). 3. Seller gate. 4–5. Classify + filter buyer. 6–7. Normalize. 8. Self-transfer
(canonical). 9. Aggregate by canonical with `count_distinct = doc_number` → `buyers_by_documents`.
10. Export (metric renamed `distinct_document_ids`).

## 04 — Buyers, gazetteer-matched (entity-accurate leaderboard)  · fixed in this audit
Groups by the gazetteer **id**, so name variants of one buyer collapse. → `matched_buyers.csv`.
1. Conveyance gate. 2. Housekeeping gate **incl. `doc_kind starts_with "B"`** *(added by Finding A —
now grant-gated like 03)*. 3. Seller gate. 4–5. Classify + filter buyer. 6. Buyer gazetteer flag.
7. Keep only rows where `assignee_names_matched == true` (buyer must resolve). 8. Self-transfer
**by id**. 9. Aggregate by `assignee_names_assignee_id, assignee_names_disambiguated` with
`count_distinct = doc_number` → `matched_buyers`. 10. Export.

## 05 — Buyers, off-gazetteer (NPEs / shells) for review
The mirror of 01/04: keeps buyers that **don't** resolve. → `flat.csv` for manual review.
1–2. Gates. 3. Seller gate (scored, `review<95`). 4–5. Classify + filter buyer. 6. Buyer gazetteer
flag (scored). 7. Keep `assignee_names_matched == false` (unmatched buyers only). 8–9. Normalize.
10. Self-transfer (canonical). 11. Sort by `assignee_names_canonical`. 12. Export selected/renamed cols.

## 06 — Firm-to-firm buyers (rules only, no reference file)
Needs no gazetteer — uses the rules classifier as the firm gate. → `flat.parquet`.
1–2. Gates. 3. **Transfer type `company → company`** (both parties must classify as firms).
4–5. Normalize. 6. Self-transfer (canonical). 7. Derive year. 8. Export.

## 07 — CPC patent list per buyer (bridge for downstream CPC match)
Row-level, one row per patent per confirmed buyer — the input to template 09. → `flat.csv`.
1–2. Gates. 3. Seller gate. 4–5. Classify + filter buyer. 6. Buyer gazetteer flag. 7–8. Normalize
(produces `assignee_names_canonical`, the buyer key 09 needs). 9. Self-transfer **by id**.
10. Derive year. 11. Export the per-patent buyer bridge.

## 08 — CPC enrich (firm-to-firm buyers + CPC codes)
Rules-only firm-to-firm, then attaches CPC by online/cached lookup. → `flat.parquet` with CPC.
1–2. Gates. 3. Transfer type `company → company`. 4–5. Normalize. 6. Self-transfer (canonical).
7. Derive year. 8. **Fetch CPC** (`flat.doc_number → cpc_codes/cpc_subclasses/cpc_lookup_status`;
offline unless the run enables network). 9. Export.

## 09 — CPC match to sales-package portfolio  · needs a processed input
Ranks buyers against a portfolio patent list. → `matched_buyers_by_portfolio_patent.csv` + overall.
1. **CPC match** (`patent_list`, `portfolio.txt`; reads `cpc_codes` + `assignee_names_canonical`).
2. Export both output tables. *Runs on the processed output of 07/08 — see Findings D/E; `portfolio.txt`
is a placeholder to replace.*

## 10 — Dropped sellers audit (off-gazetteer assignors)
Surfaces assignors the gazetteer can't confirm, for review. → `flat.csv`.
1–2. Gates. 3. Seller reference_match **`flag`** (not `keep_matched`) — scored, `review<95`.
4. Keep `assignor_names_matched == false`. 5. Export unmatched sellers with best gazetteer score.

## 11 — Attach CPC from file (firm-to-firm, offline PatSeer join)  · fixed in this audit
Rules-only firm-to-firm, CPC joined from an uploaded file, fully offline. → `flat.csv` with CPC.
1. Conveyance gate. 2. Housekeeping gate (incl. `doc_kind starts_with "B"`). 3. Transfer type
`company → company`. **4–5. Normalize assignor + assignee (match-only)** and **6. self-transfer by
canonical** *(Finding B — replaced the old raw-name compare with 13's normalize→canonical gate)*.
7. **Attach CPC file** (`patseer_export.csv` → `cpc_codes…`). 8. Derive year. 9. Export (now includes
`assignee_names_canonical`). *`patseer_export.csv` is a placeholder to replace.*

## 12 — Convert to Parquet (all tables)
No filtering — just re-emits every table. → all tables as parquet. Pair with **Convert mode**.
1. Export (all tables, parquet).

## 13 — Attach CPC from file → Parquet (offline, ready for match)
Reproducible twin of 11 producing the input for 14. → `flat.parquet` with CPC.
1–2. Gates (incl. grant `B`). 3. Transfer type `company → company`. 4–5. Normalize **match-only**
(reproducible). 6. Self-transfer (canonical). 7. Attach CPC file. 8. Derive year. 9. Export parquet
(carries `assignee_names_canonical` + CPC trio).

## 14 — CPC match (offline footprint) + per-class matches  · needs a processed input
Matches a portfolio footprint offline and emits per-class evidence. → three CSVs.
1. **CPC match** (`footprint_file`, `portfolio_footprint.csv`, `emit_class_matches`). 2. Export
`matched_buyers_by_portfolio_patent`, `matched_buyers_overall`, `matched_cpc_classes`. *Runs on 13's
output; `portfolio_footprint.csv` is a placeholder.*

---

## Canonical reviewed set (`buyer_identification_templates.reviewed.json`)
The docs-designated canonical set; all `reference_match` steps now use `reference.parquet` (Finding C),
normalize is **match-only** (reproducible), and self-transfer removal adds a **fuzzy (≥92)** pass.
1. **Strict gate, enriched** — 01 with id-based **and** fuzzy-canonical self-transfer removal (steps
   9–10) and buyer/seller-renamed export.
2. **Recall gate (unmatched = unconfirmed)** — classifies both parties by rules, only *flags* (not
   requires) gazetteer hits, so more deals survive with weaker confirmation.
3. **Distinct granted documents** — hardened 03: grant-gated, id-based self-transfer, counts distinct
   granted docs → `buyers_by_granted_docs.csv`.
4. **Deals closed** — hardened 02: id self-transfer + dedupe reel/frame before counting →
   `buyers_by_deals.csv`.
5. **Rules only, no reference file** — hardened 06: fuzzy (≥92) self-transfer instead of exact.

## Pre-review baseline (`buyer_identification_templates.json`) — kept for comparison
Four templates mirroring 01/03/02/06 but with a single-spelling conveyance filter, no `purge_indicator`
gate, and (mostly) exact self-transfer removal. **Intentionally** left on the 1.1 GB
`g_assignee_disambiguated.tsv` as the "before" reference point; prefer the numbered set or the reviewed set.

## Teaching examples (`examples.json`)
Small recipes demonstrating individual building blocks (also intentionally on the TSV):
1. **Enrich flat** — normalize + classify both parties.
2. **Firm-to-firm, enriched** — date-range filter, normalize, classify, `transfer_type`, year, export.
3. **Individual-to-company** — `transfer_type individual → company`.
4. **Remove self-transfers** — normalize then canonical compare `drop_matches`.
5. **Top assignees** — normalize, grant filter, aggregate `count_distinct doc_number`, sort desc.
6. **Assignments per year** — derive year, dedupe reel/frame, aggregate by year, sort.
7. **Reference-match assignors** — a bare `keep_matched` gazetteer gate.
