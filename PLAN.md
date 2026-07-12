# PLAN — Buyer-identification capability (entity resolution + transaction ledger)

**Status: IMPLEMENTED** (approved and built). Modules: `conveyance.py`, `propid.py`,
`dictionary.py`, `resolution.py`, `ledger.py` + CLI subcommands
(`ingest / build-dictionary / resolve / ledger / report`); DuckDB added as the query layer; docs in
`GUIDE.md` §3b. Verified end-to-end on the real bulk file + the real 516k-org PatentsView seed
(35 s dictionary build; 11,915 mentions → exact 2,643 / fuzzy 569 / person 8,013 / provisional 690;
461 firm-to-firm of 4,043 assignment transactions; plausible leaderboard with `[provisional]`
flagging). **Deliberate deviations:** OCE bulk-table ingest deferred (no OCE files locally to
validate a reader against — the generic `--extra` source adapter and dataset-dir ingest cover the
interim); residual-fuzzy parallelization deferred (the capped-block cascade resolved the real sample
in seconds single-threaded; the ProcessPool pattern from `batch.py` is the ready template when
volumes demand it). The original Stage-1 plan follows for reference.

---

## §Audit — what exists (verified against source, not assumed)

### A1. Core modules & the batch extension points

| Module (`src/uspto_assignments/`) | What it actually does |
|---|---|
| `model.py` | Row dataclasses (`Assignment`, `Assignor`, `Assignee`, `PropertyRow`), `TABLE_TYPES`, `FLAT_COLUMNS`, all-string PyArrow schemas, `columns_for()`. |
| `parser.py` | Streaming `lxml.iterparse` over `.xml`/`.zip` (reads the XML member without extraction), schema-tolerant extractors, `iter_records()`. |
| `tables.py` | `parse_to_store()` streams records → per-table Arrow-IPC files (memory-mapped `TableStore`); `flat_rows()` denormalizes (one row per property, `"; "`-joined names, latest-signer `execution_date` rollup); `open_dataset()` auto-detects Arrow/Parquet. Can **skip unbuilt tables** (`tables=` param). |
| `filters.py` | Vectorized `pyarrow.compute` filtering: 6 ops (`contains/equals/starts_with/not_empty/is_empty/in_range`), AND/OR, quick search, sort. Column-vs-literal only. |
| `exporters.py` / `writers.py` | Parquet/CSV/XLSX/JSON/Feather export (per-table or multi-sheet); streaming writers. |
| `normalize.py` | `EntityMemory`: canonicals + alias map, `clean()` (uppercase/strip-punct), **first-4-char prefix blocking**, 7 rapidfuzz scorers, `resolve()` (learn) / `match()` (pure), JSON persistence, edit API (rename/merge/delete rebuilds the block index). `normalize_column()` resolves once per **distinct** value. |
| `classify.py` | Rule-based `company/individual/unknown` (legal-suffix + org-keyword tokens, `LAST, FIRST` person form) + optional `probablepeople` ML fallback; multi-party combine modes. |
| `reference.py` | PatentsView gazetteer **loader**: streams a multi-GB TSV/CSV/Parquet (`pyarrow.csv.open_csv`, 64 MiB blocks) into an `EntityMemory` of distinct orgs + `org → assignee_id` map; per-run mtime-keyed cache; `extract_distinct_reference()` compact-Parquet builder; `match_column()` (distinct-value dictionary-encode → per-part match → `_disambiguated`/`_matched`/`_assignee_id`). |
| `batch.py` | The step engine: 12 step kinds, `BatchTemplate` JSON round-trip, `columns_after()` (schema propagation), `validate_template()`, `run_batch()` (sequential + ProcessPool parallel, folder-per-source, run log), `run_preview()` (sampled dry-run with per-step `StepStat`), silent-empty guard. |
| `cli.py` | `uspto-assign` — single-purpose: parse XML/ZIP → Parquet/Excel. Flags: `--outdir`, `--formats`, `--basename`, `--batch-size`, `-v`. **No subcommand structure yet** (argparse without subparsers). |

**How a new batch step kind is added** (six code touchpoints + three UI touchpoints, all verified by
having added five step kinds this way): dataclass with `kind` discriminator + `to_dict()` →
`_decode_step()` branch → `_apply_<kind>()` → `_apply_step()` dispatch → `_needed_tables()` +
`columns_after()` entries → `__init__.py` export; UI: a `*StepDialog` + `_STEP_DIALOGS` /
`_EDIT_DIALOGS` registries + `_describe_step()`.

### A2. Data model (confirmed columns)

Five tables, **every value a verbatim string** (leading zeros preserved; dates are `YYYYMMDD` text,
partial dates like `20240000` occur — GUIDE §1 discipline confirmed in `model.py`/`writers.py`):

- `assignments` — reel/frame header, `recorded_date`, `conveyance_text`, `purge_indicator`, correspondent fields.
- `assignors` — `reel_no, frame_no, name, `**`execution_date`**`, date_acknowledged` → **yes, per-assignor execution dates survive** at natural grain.
- `assignees` — name + full address fields.
- `properties` — `invention_title, doc_country, doc_number, `**`doc_kind`**`, doc_name, doc_date` → the
  document-type distinction survives **only as raw kind codes** (`B2`, `A1`, …). There is **no
  materialized `doc_type ∈ {application, publication, grant}` column** — it must be derived.
- `flat` — denormalized per-property view; since v7 also carries **latest-signer `execution_date`/
  `date_acknowledged` rollups**. (`flat` is a *view* for browsing; resolution should run on the
  natural-grain tables, not on `flat`.)

### A3. Existing resolution assets — the critical question

**In this repo:** exactly three things —
1. `normalize.py`'s fuzzy `EntityMemory` (learnable, blocked, scorer-selectable) — a *memory*, not a curated dictionary;
2. `classify.py`'s rule classifier (+ optional probablepeople);
3. `reference.py`'s **PatentsView disambiguated-assignee gazetteer loader** + compact-extract helper.

**NOT in this repo (verified by grep over `src/`, `tests/`, config):** no GLEIF, no SEC EDGAR, no
Wikidata, no "five-layer" normalizer, no cached multi-source gazetteers, no prebuilt
`name → entity_id` dictionary, **no parent/ownership data of any kind**. The PatentsView TSV itself
(`reference/g_assignee_disambiguated.tsv`, 1.1 GB, 516,032 distinct orgs) is an **external
user-downloaded file** in a git-ignored folder — the *loader* is ours, the *data* is not.

→ **Consequence:** the standalone resolution dictionary and its `build-dictionary` command must be
built from scratch. What's reusable is real, though: `clean()`, the blocking design (needs the
scale-hardening below), the scorer registry, the person detector, the streaming TSV reader, and the
distinct-value matching pattern.

### A4. Engines & performance today

- **DuckDB: not a dependency. Polars: not a dependency.** Declared deps: `lxml≥5`, `pyarrow≥15`
  (installed: 25.0.0), `openpyxl≥3.1`, `rapidfuzz≥3` (installed: 3.14.5); extras `ui` (PyQt6),
  `ml` (probablepeople), `dev`. `pandas` 3.0.3 is **present in the venv but undeclared** (ambient;
  rapidfuzz imports it optionally — it appears in native crash traces for that reason).
- Pipeline substrate is **pyarrow end-to-end**: lxml → dataclasses → RecordBatches → memory-mapped
  Arrow IPC store; filtering/aggregation via `pyarrow.compute`.
- Measured on the real 1.4 GB annual XML (48,044 assignments → 3.97 M flat property rows): parse
  ≈ 200 s single-threaded at ~0.7 GB peak; distinct-value normalize/classify of `flat` ≈ seconds;
  gazetteer build from the 1.1 GB TSV: 516k orgs in 26 s at ~1.4 GB peak.

### A5. Quality gate & separation

`ruff` clean, `pyright` strict clean, **180 tests green** (run this session). Qt-free core is
enforced by import direction (`uspto_assignments_ui` imports core; never the reverse) — the new
capability must live in core (or a sibling core package) with zero Qt imports.

### A6. Known scale defect (found this session, diagnosis in flight)

Matching real bulk names against the **full 516k-org gazetteer** SIGSEGVs reproducibly in the native
rapidfuzz path (`scorer=token_set`; the earlier v5 verification used a 14k proxy and never hit it).
Working hypothesis: **oversized 4-char prefix blocks** degrade `extractOne` to near-full scans
(stability + O(n²) + precision problem at once). Block-size instrumentation is running; results land
with this plan. Whatever the root cause, **Phase 5's blocked-fuzzy layer must include the block-cap /
sub-blocking hardening and a synthetic pathological-block stress fixture in CI** — this is a
prerequisite, not an afterthought.

### A7. Gap analysis

| Needed for the design | Exists / reusable | Must build |
|---|---|---|
| Streaming ingest at natural grain | ✅ XML→Arrow store (`parse_to_store`), Parquet export | OCE bulk-table ingest; Parquet landing convention |
| Transaction reconstruction (reel/frame + true date) | ✅ per-assignor `execution_date`; v7 rollup logic as reference | group-by ledger builder + `date_source` |
| Conveyance taxonomy | ❌ (substring filters only) | curated regex/lookup table + module |
| Property canonicalization | ⚠️ raw `doc_kind`/`doc_number` survive | `doc_type` derivation, `canonical_property_id`, `patent_id_normalized` |
| Exact-lookup dictionary | ❌ | build-once artifact + `build-dictionary` command |
| Blocked fuzzy on residual | ⚠️ `EntityMemory` blocking + scorers | scale hardening (block cap), parallel residual pool |
| Person detection | ✅ `classify.py` rules | wire into cascade |
| Provisional entity minting | ❌ | deterministic ids + cross-run persistence |
| Parent / ultimate-owner data | ❌ | GLEIF relationship ingest (build-time) |
| Query layer (sub-second leaderboards) | ❌ (pyarrow compute only) | DuckDB views over Parquet |
| CLI subcommands | ❌ (single-purpose CLI) | `ingest / build-dictionary / resolve / ledger / report` |

---

## §Design

### D1. Architecture in one paragraph

Everything cheap runs first; the one expensive operation (entity resolution) runs once, on the
smallest possible residual, and its results are **persisted**. Ingest lands raw tables at natural
grain as Parquet → conveyance taxonomy cuts rows early → transactions are reconstructed per
reel/frame with the true (execution) date → property ids are canonicalized → each distinct party
mention goes through a **resolution cascade** (clean → exact hash → blocked fuzzy → person detector →
provisional mint) against a **local, build-once dictionary artifact** → a firm-to-firm predicate on
*entities* (not strings) → a versioned `transaction_ledger` Parquet → `buyers` and
`buyer_property_bridge` are derived views/materializations, and leaderboards are **DuckDB queries
over Parquet, not re-runs**. No network calls at pipeline run time, ever.

### D2. Engine recommendation — **DuckDB** (decision 1, my recommendation)

Justified from the audit: the entire existing substrate is **pyarrow/Parquet**, and DuckDB is
zero-copy over Arrow tables and Parquet files — it slots in as a *query layer* without disturbing a
single existing module. It gives: SQL group-by/join for ledger reconstruction and sub-second
leaderboards over materialized Parquet; out-of-core spilling for multi-GB joins; direct
`read_csv`/`read_parquet` of OCE bulk tables; one small self-contained dependency. Polars would add a
second dataframe idiom alongside pyarrow (API churn, two ways to do everything) and brings no query
layer. Nuance: the **exact-lookup layer is a hash-map problem, not a dataframe problem** — the
dictionary loads into an in-process Python dict (516k–2M keys is trivial RAM); DuckDB handles the
relational stages and every downstream query.

### D3. The standalone dictionary artifact (decision 2 + 4, my recommendation)

- **Format: Parquet, not SQLite** — Arrow-native (mmap, zero-copy into the tool), columnar, diff-able
  by manifest, no transactional-write requirement. Layout under git-ignored `dictionary/`:
  - `entities.parquet` — `entity_id, canonical_name, entity_type, country, sector, source, ultimate_parent_id`
  - `aliases.parquet` — `alias_key (cleaned), entity_id, alias_source, confidence`
  - `provisional.parquet` — minted off-gazetteer entities (see below)
  - `manifest.json` — source files, versions, sha256 hashes, build timestamp, row counts.
- **`uspto-assign build-dictionary`** — explicit, separate command. Each seed source is **optional
  and local**: `--patentsview <g_assignee_disambiguated.tsv>` (already on disk; 516k orgs + ids),
  `--gleif <golden-copy csv>` (legal names + **relationship file → ultimate_parent_id**),
  `--sec <company_tickers.json>`, `--wikidata <curated subset>`. The command normalizes all names
  through `clean()`, merges by exact key with source precedence, and writes the artifact. An
  optional `--download` flag may fetch files **at build time only**; the pipeline itself only ever
  reads the artifact. Ship with **PatentsView-only** as the v1 seed (it's already here and covers the
  patent domain); GLEIF/SEC/Wikidata are additive build inputs, not blockers.
- **Provisional ids (stable across runs):** unresolved org mentions are clustered against each other
  (same blocked-fuzzy machinery, high threshold); each cluster's id is
  `prov-<sha1(cleaned name of the deterministic representative)>` where the representative is the
  lexicographically-smallest cleaned member — deterministic for a given input set. Minted ids are
  **written back into `provisional.parquet`**, so subsequent/incremental runs re-resolve the same
  names to the same ids by exact lookup. If new data later merges two provisional clusters, the merge
  is recorded as an alias remap in the artifact (old id → surviving id) rather than silently changing.

### D4. Integration form — **new subcommands, not batch steps** (decision 3, my recommendation)

The batch step engine operates on *one working set of tables per input file* and re-runs end-to-end.
This capability is a **multi-table pipeline with persisted, versioned intermediates and an
incremental mode** — forcing it into step kinds would fight the engine (no cross-run state, no
natural home for the ledger). Recommend:

```
uspto-assign ingest            <xml|zip|oce-dir> --out data/raw/        # land Parquet at natural grain
uspto-assign build-dictionary  --patentsview ... [--gleif ...] ...      # build-once artifact
uspto-assign resolve           --raw data/raw --dict dictionary/ --out data/resolved/   # mentions → entity_ids
uspto-assign ledger            --resolved data/resolved --out data/ledger/              # A + B + C tables
uspto-assign report            --ledger data/ledger [--top 100] [--cpc-mode sampled|full]
```

(Implemented as argparse subparsers; the existing bare parse invocation stays as the default
subcommand for backward compatibility.) The UI keeps its existing steps untouched; later, a thin
`resolve` batch step can delegate to the dictionary for interactive users — listed as optional.

### D5. Phases (each a testable unit, in order)

1. **Land raw at correct grain** — `ingest` writes `assignments/assignors/assignees/properties`
   Parquet from either (a) the existing XML parser (reuse `parse_to_store` → Parquet export; parse
   **once**) or (b) **OCE bulk research tables** (new thin reader; DuckDB `read_csv` → Parquet).
   Prefer OCE for annual data (it already splits app/pub/grant and carries execution dates); XML for
   fresher-than-annual. No flattening.
2. **Transaction reconstruction** — one DuckDB query: group `assignors` by reel/frame,
   `transaction_date = max(execution_date)` with `date_source='execution'`, fallback
   `recorded_date`/`'recorded'`. Unit tests on fixtures with multi-signer + missing-date records.
3. **Conveyance taxonomy** — `conveyance.py` + an in-repo, versioned rules file (regex → `conveyance_type`
   ∈ {assignment, security_interest, release, merger, name_change, license, correction,
   nunc_pro_tunc, other}). Seeded from an `aggregate` over the real file's vocabulary. Early row cut.
4. **Property canonicalization** — derive `doc_type` from `doc_kind` (A\*=publication, B\*=grant,
   S/P/E/H/RE design-plant-reissue routing, plus number-shape heuristics for blank kinds);
   `canonical_property_id` = application number where derivable, else normalized grant/pub id;
   `patent_id_normalized` formatted to the CPC target convention (**decision 5 — blocked on your
   answer**). Always carry the raw number alongside.
5. **Entity resolution** — the core. Per distinct mention: `clean()` + transliteration map + legal-
   suffix strip (recorded) → blocking key → **exact dict lookup** (expected to resolve the bulk) →
   residual **blocked fuzzy** with the **block-cap/sub-blocking hardening** (extend prefix to 6–8
   chars or sub-block by token when a block exceeds a threshold) and the pathological-block **stress
   fixture in CI** → person detector → **provisional mint + residual-vs-residual clustering**.
   Emits per mention: `entity_id, entity_type, resolution_source
   (exact|fuzzy|person|provisional), resolution_confidence, ultimate_parent_id`. Residual fuzzy
   parallelized with the existing ProcessPool pattern. Confident fuzzy matches written back as
   aliases (into the artifact's alias table — explicit, versioned; **not** the UI's learnable memory,
   preserving reproducibility).
6. **Firm-to-firm predicate on entities** — keep a transaction iff every kept buyer resolves to an
   org AND every kept seller resolves to an org AND
   `seller.ultimate_parent_id != buyer.ultimate_parent_id` (falling back to `entity_id` inequality
   where parents are unknown). Inventors drop out as `person`; `ACME INC → ACME HOLDINGS` drops via
   parent equality when GLEIF is loaded, via provisional-cluster equality otherwise.
7. **Transaction ledger** — contract A below, one row per reel/frame, written as versioned Parquet
   (`ledger/transaction_ledger.parquet` + manifest). Source of truth; everything downstream is a view.
8. **Buyer aggregation + bridge** — contracts B and C below; leaderboards at both honest grains
   (deals = distinct reel/frame; patents = distinct `canonical_property_id`); buyer profile columns;
   `cpc_lookup_status` initialized; **`cpc_hit_rate`** emitted as a run metric after any external CPC
   join (fail-loudly reconciliation); `--cpc-mode sampled|full` shapes the bridge (cap N most recent
   grants per buyer vs all grants).

### D6. Data contracts (targets — exact schemas from your spec)

Emitted at `ledger/`, joinable on `entity_id`; all identifier/date columns verbatim strings, with
explicitly-typed derived columns only inside DuckDB views:

- **A `transaction_ledger`** (per reel/frame): `reel_no, frame_no, conveyance_type,
  conveyance_text_raw, transaction_date, date_source, recorded_date, seller_entity_ids,
  seller_canonical_names, buyer_entity_ids, buyer_canonical_names, seller_parent_ids,
  buyer_parent_ids, property_count, correspondent_name`. (Multi-valued id/name fields as Arrow
  `list<string>` — real lists, not joined strings.)
- **B `buyers`** (per resolved buyer entity): `entity_id, canonical_name, entity_type,
  resolution_source, resolution_confidence, ultimate_parent_id, ultimate_parent_name, country,
  sector, deals_count, patents_count, first_acquisition_date, last_acquisition_date,
  is_off_gazetteer`.
- **C `buyer_property_bridge`** (per buyer × property; **the CPC feed**): `entity_id, reel_no,
  frame_no, canonical_property_id, doc_number_raw, kind_code, doc_type, patent_id_normalized,
  doc_date, invention_title, transaction_date, cpc_lookup_status` (empty → `found/not_found/na`;
  app/pub rows marked `na`-eligible so grant-only routing is explicit — no silent empty joins).

### D7. Performance targets (concrete)

- Full annual bulk (1.4 GB XML / 48k assignments / ~131k party mentions / ~39k distinct org names):
  **ingest ≤ 4 min** (existing parser pace), **resolve ≤ 2 min** on 8 cores with ≥ 90 % of mentions
  via exact lookup and only the residual entering capped-block fuzzy, **ledger + aggregates ≤ 30 s**.
- Leaderboard/profile queries over the materialized ledger: **< 1 s** (DuckDB over Parquet).
- Incremental daily mode: resolve **only genuinely new** distinct names (exact-lookup hit on
  everything seen before, including provisionals) — target < 30 s/day-file end-to-end.
- Dictionary build (PatentsView 1.1 GB + GLEIF): one-off, minutes; never on the run path.

---

## §Risks

1. **Native fuzzy path segfault at scale** (A6) — mitigated by block-cap + input sanitation + CI
   stress fixture *before* Phase 5 relies on it; if instrumentation shows uniform blocks instead,
   escalate to rapidfuzz pin/upstream (different fix, same phase gate).
2. **Seed-source licensing/size** — GLEIF golden copy is multi-GB and Wikidata needs a curated
   subset; mitigated by making every source optional and shipping PatentsView-only v1.
3. **Provisional-id stability under cluster merges** — deterministic minting + recorded remaps
   (D3); still a documented behavior, not magic.
4. **App→grant linkage** — assignment data alone can't map an application row to its later grant;
   the bridge marks `doc_type` honestly and leaves resolution to your downstream stage (flagged, not
   hidden).
5. **Parent hierarchy coverage** — `ultimate_parent_id` is only as good as GLEIF relationship data;
   PatentsView has none. The predicate degrades gracefully to entity-id inequality + provisional
   clustering.
6. **OCE format drift** — bulk research tables change columns across releases; ingest treats column
   maps as config, mirrored from the reference-match `name_column` pattern.
7. **`pandas` ambient in the venv but undeclared** — either declare it or ensure nothing imports it;
   with pyarrow 25 + pandas 3.0.3, version-skew in native paths is a real crash vector (see A6
   diagnosis).

## §Optional features (proposed, not built — pick any)

| Feature | What it adds | Effort |
|---|---|---|
| Confidence-gated review queue | auto-accept ≥ high threshold; ambiguous residual → CSV/UI review list; accepted pairs merge into the artifact | M (2–3 d) |
| Ownership-chain reconstruction | per-property chain of custody across transactions; enables "current owner" queries | M–L (3–5 d) |
| Buyer-intent / aggregator signal | flags buyers whose acquisitions cluster in one CPC class and who never file originals (needs your CPC join output back) | S–M (1–2 d) |
| Incremental daily ingestion | watch/append daily dumps; only-new-names resolution; ledger append with dedup | M (2–3 d) |
| Thin `resolve` batch step for the UI | exposes dictionary lookup inside the existing template system | S (1 d) |

## §Decisions needed from you (recommendations attached)

1. **Engine:** I recommend **DuckDB** (query layer over the existing pyarrow/Parquet substrate;
   Polars would duplicate the dataframe idiom). Confirm or override.
2. **Dictionary storage:** **Parquet artifact set + manifest** in git-ignored `dictionary/` (over
   SQLite), sources fetched/staged **at build time only**. Confirm.
3. **Integration:** **new subcommands** (`ingest / build-dictionary / resolve / ledger / report`),
   batch steps untouched; optional thin UI step later. Confirm.
4. **Provisional ids:** deterministic `prov-<sha1(representative cleaned name)>`, persisted to the
   artifact, merges recorded as remaps. Confirm.
5. **CPC source:** I need the exact target — **PatentsView `g_cpc_current` (patent_id: grant number,
   no country prefix, no leading zeros, D/PP/RE prefixes for design/plant/reissue)** or the **USPTO
   CPC Master Classification File** (13-char zero-padded document ids)? `patent_id_normalized` will
   be formatted to whichever you name.

---

**STOP.** Awaiting your review of this plan (and your read of the segfault block-distribution data,
delivered alongside) before any implementation.
