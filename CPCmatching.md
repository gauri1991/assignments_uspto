# CPC Matching — complete guide

How the tool attaches **CPC classification codes** to patents and matches acquired patents against a
**target portfolio** by CPC overlap — including the fully **offline** workflow and the **many-to-many
class-match** output. This is the authoritative reference for every CPC template.

- [1. The big picture](#1-the-big-picture)
- [2. Building blocks (the three CPC steps)](#2-building-blocks-the-three-cpc-steps)
- [3. How matching actually works](#3-how-matching-actually-works)
- [4. Output tables](#4-output-tables)
- [5. The many-to-many class-match table](#5-the-many-to-many-class-match-table)
- [6. File formats you provide](#6-file-formats-you-provide)
- [7. The CPC templates](#7-the-cpc-templates)
- [8. The offline 13 → 14 walkthrough](#8-the-offline-13--14-walkthrough)
- [9. Configuration knobs](#9-configuration-knobs)
- [10. Troubleshooting](#10-troubleshooting)

---

## 1. The big picture

CPC work has **two independent stages**, each with its own file input:

```
STAGE 1 — attach CPC codes to YOUR patents        STAGE 2 — match against a target portfolio
  fetch_cpc  (API or local bulk file)               cpc_match
  attach_cpc_file  (your PatSeer/CSV export)          portfolio = patent list OR footprint file
        │                                                     │
        ▼                                                     ▼
  adds cpc_codes / cpc_subclasses / cpc_lookup_status   ranks buyers per portfolio patent
                                                        (+ optional per-class match table)
```

A patent's CPC symbols are **classification codes** like `G06F 16/2455`. Matching reduces every symbol
to a **grain** and compares patents by the **set of grain codes** they share.

---

## 2. Building blocks (the three CPC steps)

| Step | What it does | Where CPC comes from | Network? |
|---|---|---|---|
| **`fetch_cpc`** | Adds `cpc_codes` to a patent-number column | The **configured CPC data source** (Settings ▸ CPC / USPTO API data source): either the live **USPTO ODP API** or a **local bulk file** | API = yes; local file = no |
| **`attach_cpc_file`** | Same three columns, but joined from **a file you name in the step** | The file's `source_path` (PatSeer/CSV/TSV/Parquet) | **Never** |
| **`cpc_match`** | Ranks buyers by CPC overlap against a portfolio | Reads the `cpc_codes` a prior step attached; resolves the *portfolio* side from `portfolio_path` | Only `patent_list` mode may fetch |

All three route patent numbers to **grants** (CPC is grant-only) and normalize them to the bare grant
number before any join. User-supplied CPC/portfolio/footprint files may key patents in
**publication style** (`US10987654B2`, `USD912345S1`, comma-grouped digits) — those are normalized
to the same bare-grant key too, so a raw PatSeer "Publication Number" column joins directly. Each of
`fetch_cpc` / `attach_cpc_file` adds:

- **`cpc_codes`** — list of full CPC symbols (e.g. `["G06F16/2455", "H04L9/32"]`)
- **`cpc_subclasses`** — list of 4-char subclasses (e.g. `["G06F", "H04L"]`)
- **`cpc_lookup_status`** — `found` / `not_found` / `uncached` / `na` (non-grant)

> **Always check `cpc_lookup_status`** after attaching. If most rows are `not_found`, your source file
> didn't cover those patents (or the patent-number formats disagree) — fix that before matching, because
> `cpc_match` aborts when the CPC hit-rate is below the floor.

---

## 3. How matching actually works

1. **Grain reduction.** Every CPC symbol is reduced to the configured `grain`:
   - `subclass` (default) → `G06F16/2455` becomes **`G06F`**
   - `main_group` → **`G06F16`**
   - `full_symbol` → **`G06F16/2455`**
2. **Footprints.** Each portfolio patent becomes a **set** of grain codes (its "footprint").
3. **Buyer patents.** Each acquired patent (one row per buyer × patent) becomes a set of grain codes.
4. **Overlap.** For every `(portfolio patent × buyer patent)` pair the matcher computes the **set
   intersection** `footprint & buyer_codes`. A pair "matches" when the overlap **score** ≥
   `overlap_threshold`. Score depends on `overlap_metric`: `shared_count` (default = number of shared
   classes), `jaccard`, or `rarity_weighted`.
5. **Roll-up + rank.** Matches roll up to buyers; each buyer is ranked by
   `weight_overlap·strength + weight_volume·in_domain_patents + weight_recency·recency`.

> **Important:** because matching is a set **intersection**, a class only ever matches **the identical
> class** at the chosen grain — there is no cross-class similarity (`G06F` never matches `H04L`). Choose
> a coarser grain (`subclass`) for broader matches, a finer grain (`main_group` / `full_symbol`) for
> stricter ones.

---

## 4. Output tables

`cpc_match` writes two tables (plus the optional class table in §5):

**`matched_buyers_by_portfolio_patent`** — one row per `(portfolio patent, buyer)`:

| column | meaning |
|---|---|
| `portfolio_patent` | the target/sales-package patent |
| `buyer` | the acquiring firm (canonical) |
| `overlap_strength` | summed overlap score across the buyer's matched patents |
| `in_domain_patents` | how many of the buyer's patents matched this portfolio patent |
| `last_acquisition_date` | latest matched-deal year |
| `shared_codes` | **union** of the CPC classes that overlapped (all patents combined) |
| `is_off_gazetteer` | `true` for provisional (un-disambiguated) buyers |
| `rank_score`, `rank` | the ranking score and 1-based rank within this portfolio patent |

**`matched_buyers_overall`** — one row per buyer, rolled up across the whole portfolio
(`portfolio_patents_matched`, `total_overlap_strength`, `in_domain_patents`, `last_acquisition_date`).

Note the per-patent table's `shared_codes` is a **union** — it tells you *which classes* linked a buyer,
but not *which patent* or the class-by-class breakdown. That granularity is the class-match table.

---

## 5. The many-to-many class-match table

Enable **"Also output per-class matches"** on the `cpc_match` step (`emit_class_matches: true`) to also
write **`matched_cpc_classes`** — the fully granular evidence, **one row per
`(portfolio patent × buyer patent × shared CPC class)`**:

| portfolio_patent | buyer | buyer_patent | cpc_class | year | is_off_gazetteer |
|---|---|---|---|---|---|
| 9111111 | ACME | 10123456 | G06F | 2019 | false |
| 9111111 | ACME | 10123456 | H04L | 2019 | false |
| 9222222 | ACME | 10234567 | H04L | 2020 | false |

- **One row per class**, so a patent pair sharing two classes yields two rows.
- **`cpc_class`** is the shared, grain-reduced code (same grain as the match config).
- It records **exactly which buyer patent** linked to **which portfolio patent** via **which class** —
  the union `shared_codes` on the ranked table cannot show this.
- The class table is **consistent with the ranked table**: it honors the same `overlap_threshold` and
  `min_in_domain_patents` filters, so a buyer dropped from the ranking produces **no** class rows.
- Because a class matches only its identical class, every row is a genuine `class ↔ same class` hit.

This is the table to use for "which CPC classes drove each match" analysis, pivots, or a class-frequency
breakdown per buyer/portfolio patent.

---

## 6. File formats you provide

**PatSeer / CSV export** — for `attach_cpc_file` (one row per patent, CPC codes optionally packed):

```
Publication Number,CPC
10123456,G06F16/00; H04L9/32
10234567,A61K31/00
```
Configurable in the step: `patent_column` (default `Publication Number`), `code_column` (default `CPC`),
`separator` (default `;` — splits multi-code cells; blank = one code per row).

**Portfolio — footprint file** (`portfolio_mode: footprint_file`, offline) — **columns by position**:
column 1 = patent, column 2 = CPC, **one code per row**:

```
patent,cpc
9111111,G06F16/00
9111111,H04L9/32
9222222,A61K31/00
```

**Portfolio — patent list** (`portfolio_mode: patent_list`) — one grant number per line; the tool resolves
each patent's CPC via the configured source (may use the network):

```
9111111
9222222
```

Relative paths in a template resolve from where you launch the app; **Browse** in the dialogs to store
absolute paths.

---

## 7. The CPC templates

| Template | Role | CPC step | Files you set |
|---|---|---|---|
| **07** – CPC patent list per buyer | Firm-to-firm cleanup → a tidy buyer→patent bridge (no CPC yet) | — | — |
| **08** – CPC enrich (firm-to-firm + CPC) | Firm-to-firm deals + `fetch_cpc` from your configured source | `fetch_cpc` | CPC **source** in settings |
| **11** – Attach CPC from file | Firm-to-firm deals + `attach_cpc_file` (CSV export) | `attach_cpc_file` | PatSeer CSV (`source_path`) |
| **09** – CPC match to portfolio | Match an already-enriched table against a patent-list portfolio | `cpc_match` | portfolio list (`portfolio_path`) |
| **13** – Attach CPC from file → Parquet (offline) | Like 11 but **normalizes the buyer** and exports **Parquet** (a reopenable dataset) | `attach_cpc_file` | PatSeer CSV |
| **14** – CPC match (offline footprint) + class matches | `footprint_file` mode + `emit_class_matches` → three result tables | `cpc_match` | footprint CSV |

**Two-run design.** `cpc_match` needs `cpc_codes` already on the table (it errors *"run a fetch_cpc step
before cpc_match"* otherwise). So enrichment (08/11/13) and matching (09/14) are **separate runs**: enrich
and export, then feed that output back in as the input to the match template.

---

## 8. The offline 13 → 14 walkthrough

A completely offline pipeline: your PatSeer export supplies the CPC codes; your footprint file supplies the
target. **No network at any point.**

**Files:** a PatSeer CSV (§6) and a portfolio **footprint** CSV (§6).

**Run template 13** (attach + export a dataset):
1. **Settings ▸ Batch ▸ Import…** → `templates/13_cpc_attach_offline.json`.
2. **Add files…** → your USPTO assignment `.xml`/`.zip`.
3. Open the **Attach CPC from file** step → set **CPC file** to your PatSeer CSV (Browse); confirm the
   patent/CPC column names and `;` separator.
4. Pick an output folder → **Run**. Output: `…/run_<ts>/<source>/flat.parquet` with `cpc_codes` +
   `assignee_names_canonical` attached (Parquet keeps the `cpc_codes` **list** intact — CSV would flatten it).
5. Sanity-check `cpc_lookup_status` in that Parquet (landing page ▸ *View Parquet*).

**Run template 14** (match + class matches):
1. **Import…** → `templates/14_cpc_match_offline.json`.
2. **Add folder…** → the **run folder from step 13**. It contains `flat.parquet`, so the app auto-detects it
   as a dataset and adds it as one input.
3. Open the **CPC match** step → set **Portfolio input = Pre-built CPC footprint file**, **Portfolio file =**
   your footprint CSV, keep **Buyer column = `assignee_names_canonical`** (13 produced it), and leave **"Also
   output per-class matches"** ticked.
4. Pick an output folder → **Run**.

**Outputs:** `matched_buyers_by_portfolio_patent`, `matched_buyers_overall`, and **`matched_cpc_classes`**
(§5) as CSV.

> If you'd rather run it as one job, a single template can do `attach_cpc_file → cpc_match` in one run (the
> two-template split exists only so the enriched dataset can be inspected and reused). The bundled 13/14 keep
> them separate for auditability.

---

## 9. Configuration knobs

Match behavior comes from the **project CPC config** (Settings ▸ CPC data source), not the step:

| Setting | Default | Effect |
|---|---|---|
| `grain` | `subclass` | Matching resolution: `subclass` (G06F) / `main_group` (G06F16) / `full_symbol` |
| `overlap_metric` | `shared_count` | Score per pair: count of shared classes / `jaccard` / `rarity_weighted` (rare classes weigh more) |
| `overlap_threshold` | `1.0` | Minimum score to count a patent pair as a match (1.0 = ≥1 shared class) |
| `min_in_domain_patents` | `1` | Drop a buyer with fewer than this many matched patents for a portfolio patent |
| `hit_rate_floor` | `0.5` | Abort the match if the CPC join hit-rate is below this (a likely number-format mismatch) |
| `weight_overlap` / `weight_volume` / `weight_recency` | `1.0` each | Linear weights in the buyer `rank_score` |

The grain also determines the granularity of the **`cpc_class`** column in the class-match table.

---

## 10. Troubleshooting

- **"run a fetch_cpc step before cpc_match"** — the input table has no `cpc_codes`. Enrich first (08/11/13),
  export, then match on that output.
- **"CPC hit-rate … below the floor"** — the patents' numbers and your CPC file's patent-number format
  disagree, or the file barely covers your patents. Bare grant numbers (`10987654`) and
  publication-style ids (`US10987654B2`) both join; anything else (application numbers, PCT ids)
  will not. Check `cpc_lookup_status` and the file's patent column.
- **"all grant patents are uncached and the network is disabled"** — you're in `patent_list` mode offline
  with no cached CPC for the portfolio patents. Use **`footprint_file`** mode (supply the CPCs directly), or
  enable the network / point at a local source.
- **`matched_cpc_classes` empty** — no class actually overlapped above the threshold, or `emit_class_matches`
  is off. Verify both sides carry codes and that the grain isn't too fine.
- **Nothing matches** — try a coarser `grain` (`subclass`), lower `overlap_threshold`, or confirm the
  footprint file's codes are at a compatible grain.

---

See also: **GUIDE.md** §9.5 (CPC matching) and the batch step catalog; **templateInfo.md** for the exact
JSON step fields.
