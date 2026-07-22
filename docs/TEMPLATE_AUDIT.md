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
