# VERIFY — buyer-identification pipeline, five-item audit

Verification run **2026-07-12** on the real bulk file (`ad20260709.zip`) and the real 516k-org
PatentsView seed (`reference/g_assignee_disambiguated.tsv`). Evidence is the command + its output.

| # | Item | Verdict | One-line reason |
|---|---|---|---|
| 1 | Block-cap in **shared** blocking, not only the new cascade | ⚠️ **PARTIAL → fixed** | No live crash (empirically clean at full scale), but the cap lived only in `resolution.py`; `normalize`/`reference` blocking was uncapped |
| 2 | `report --cpc-file` puts CPC **codes** on the bridge | ❌ **FAIL → fixed** | Only `cpc_lookup_status` + `cpc_hit_rate` were emitted; no CPC-code column |
| 3 | Rolled-up execution date on `flat`, documented | ⚠️ **PARTIAL → fixed** | `execution_date` existed + populated, but no `date_source` flag / `recorded_date` fallback for time-axis use |
| 4 | Provisional ids/clusters deterministic across runs | ✅ **PASS** (test added) | Two fresh builds → identical ids; content-hash minting over sorted inputs |
| 5 | Path positioning & template-file sprawl | ⚠️ **PARTIAL → fixed** | CLI-vs-template positioning existed; shipped templates lacked a scale note; reviewed set not marked canonical |

---

## Item 1 — shared blocking cap (priority)

**Structural finding.** The cap/re-split lived only in `resolution.py::CappedBlockIndex`. The shared
`EntityMemory` (used by `normalize` **and** the `reference` / Match-against-reference step) blocked on
a plain fixed 4-char prefix with **no cap**:

```
$ grep -n "_BLOCK_CHARS\|_blocks.setdefault\|def _fuzzy_match" src/uspto_assignments/normalize.py
31:_BLOCK_CHARS = 4
116:        self._blocks.setdefault(key[:_BLOCK_CHARS], []).append(index)   # uncapped
120:        indices = self._blocks.get(key[:_BLOCK_CHARS])                   # uncapped
```

**Empirical finding — no live crash.** Match-against-reference on the FULL 516k gazetteer with a
realistic set of distinct names completes cleanly and fast (the earlier SIGSEGV was the empty-table
pyarrow kernel, already fixed in `filters.py` — not blocking):

```
$ python … build_reference(full tsv) ; match_column(flat, token_set@90)
gazetteer built: 516,032 orgs in 46s
input: 134,120 flat rows, 4,069 distinct assignor party names
MATCH-AGAINST-REFERENCE (full 516k gazetteer, token_set@90): OK — matched 122,708 of 134,120 rows in 1.1s
NO SIGSEGV
```

Real-world block sizes are benign (max block `THE ` = 7,265 ≈ 1.4 % of the gazetteer), so there is no
crash and no perf cliff today. **But** the pass criterion is "shared blocking is capped AND runs
clean" — the cap was not shared, leaving a *synthetic* pathological gazetteer (tens of thousands of
orgs behind one 4-char prefix) unguarded on the `normalize`/`reference` path.

**Verdict: PARTIAL.** Fixed by moving the cap into the shared block index (Item-1 fix below).

## Item 2 — CPC codes on the bridge (priority)

`reconcile_cpc` only tested existence and wrote a status flag — it never joined the actual CPC codes
onto `buyer_property_bridge`:

```
$ python -c "print(pq.read_schema('data/ledger/buyer_property_bridge.parquet').names)"
['entity_id','reel_no','frame_no','canonical_property_id','doc_number_raw','kind_code','doc_type',
 'patent_id_normalized','doc_date','invention_title','transaction_date','cpc_lookup_status']
has any cpc code column: False
```

Portfolio-vs-buyer CPC matching needs the codes per patent, not just found/not_found. **Verdict:
FAIL.** Fixed by attaching `cpc_codes` (full symbols) + `cpc_subclasses` (4-char) in both cpc-modes.

## Item 3 — execution date on `flat`

`execution_date` and `date_acknowledged` **are** on `flat`, populated with latest-signer logic:

```
$ grep -n "execution_date" src/uspto_assignments/model.py src/uspto_assignments/tables.py
model.py:111:    "execution_date",         # in FLAT_COLUMNS
tables.py:49:    execution_date = max((a.execution_date for a in rec.assignors if a.execution_date), default=None)
```

Gaps vs the pass criterion: no `date_source` flag and no `recorded_date` fallback, so time-axis work
can't key off a single always-present true-date column. **Verdict: PARTIAL.** Fixed by adding
`transaction_date` (execution date, else recorded date) + `date_source` to `flat` (leaving
`execution_date` pure), documented in GUIDE §1.

## Item 4 — provisional determinism

Two **fresh** dictionary builds + resolves of the same names produce identical ids, and legal-form
variants collapse to one cluster:

```
$ python … build_dictionary(); resolve_mentions(names)  # twice, fresh dirs
run1==run2 ids: True
  prov-c111afe08dcd4f5c  ZORP SYSTEMS INC
  prov-c111afe08dcd4f5c  ZORP SYSTEMS INCORPORATED   # ← same cluster
  prov-fa505a1b7393ec7d  ACME IP HOLDINGS LLC        # ← distinct (no generic over-merge)
```

Ids are `prov-<sha1(lexicographically-smallest cleaned representative)>` over `sorted()` inputs, so
minting and clustering are order-independent. **Verdict: PASS.** A run-to-run stability test was
added to lock it in.

## Item 5 — path positioning & template sprawl

`templateInfo.md` §9–10 already position CLI-vs-templates, but all three shipped template files point
at the raw multi-GB TSV with no scale note, and the reviewed set was not marked canonical:

```
$ grep -l g_assignee_disambiguated.tsv templates/*.json
templates/examples.json
templates/buyer_identification_templates.json
templates/buyer_identification_templates.reviewed.json
```

**Verdict: PARTIAL.** Fixed by a one-line positioning note + scale note in GUIDE §3b/§10 and marking
`*.reviewed.json` canonical (originals kept for diff).

---

## Final validation (after fixes)

**Quality gate:** `ruff` clean, `pyright` strict clean, **202 tests pass** (+4: shared-cap regression,
below-cap behavior-preservation, CPC-codes-on-bridge, run-to-run determinism).

### 1. Full-scale run — completes, no crash

Full `ad20260709.zip` (no limit) + the real 516k-org gazetteer. **No SIGSEGV anywhere** (the scale
that previously crashed a template preview):

```
build-dictionary : 516,032 entities / 916,551 aliases   |  0:36.6  peak 1.5 GB
ingest (full)    : 4 raw tables + flat                   |  3:48.4  peak 3.2 GB
ledger           : 90,639 mentions {exact 12,095 · person 72,624 · fuzzy 1,953 · provisional 3,967}
                   3,768 firm-to-firm of 42,083 kept txns |  0:42.5  peak 2.4 GB
```

### 2. Off-gazetteer spot-check — clustering is sound

Top provisional buyers are real acquisition/NPE shells (NEW CARCO = Chrysler, OT PATENT ESCROW, OEP
IMAGING = Kodak, WITRICITY AI TECH, SIM IP). Multi-name provisional clusters are **correct merges**
(`WAYMO HOLDING`/`HOLDINGS`, `QPX LLC`/`QPX, LLC.`, 11 `DEUTSCHE BANK AG NEW YORK BRANCH …` collateral
variants). **No generic-shell over-merge** — distinct `… IP HOLDINGS LLC` names did not collapse. One
mild case: `MAGENTA SECURITY HOLDINGS LLC` + `MAGENTA SECURITY INTERMEDIATE HOLDINGS LLC` merged
(arguably two entities).

### 3. Over-merge spot-check — ⚠️ `token_set` @ 90 over-merges some distinct firms (FLAG, not fixed)

Grouping raw names by resolved `entity_id` surfaced a real precision issue for you to judge (I did
**not** auto-tune the threshold, per instructions):

- **Worst case — genuinely distinct firms merged:** one gazetteer entity swallowed **24 different
  Shenzhen companies** (`SHENZHEN AIGAN TECHNOLOGY`, `SHENZHEN AOJ HEALTH`, `SHENZHEN BEITONG
  CONTROL`, `SHENZHEN BOTINKIT`, …). `token_set` ignores the distinguishing token when the shared
  `SHENZHEN … TECHNOLOGY CO LTD` tokens dominate. Also `BAYER AKTIENGESELLSCHAFT` merged with
  `BAYER SCHERING PHARMA AKTIENGESELLSCHAFT` (distinct entities).
- **Corporate-family merges (parent-grain judgment call):** `NOKIA CORPORATION` / `NOKIA NETWORKS
  FRANCE` / `NOKIA SOLUTIONS AND NETWORKS JAPAN`, and 8 `MALLINCKRODT …` subsidiaries, collapse to one
  id each — fine if you want parent grain, wrong if you need the legal entity.
- **Correct merges (the majority):** `JPMORGAN CHASE BANK` (18 variants), `CITIBANK N.A.` (12),
  `BANK OF AMERICA N.A.` (11), `U.S. BANK` (16) — all legitimate punctuation/agent-role variants.

**Recommendation for your review (not applied):** for entity-accurate work, tighten the ledger to
`--scorer token_sort --threshold 92`+ (order-sensitive, penalizes the extra distinguishing token) or
add a token-overlap guard; keep `token_set` when you deliberately want parent/family grain. This is a
scorer-choice trade-off (documented in templateInfo §4b), not a bug in the pipeline.
