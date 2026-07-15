"""Tests for entity-name normalization (uspto_assignments.normalize)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pyarrow as pa

from uspto_assignments import EntityMemory, normalize_column
from uspto_assignments.normalize import _BLOCK_CAP, CappedBlockIndex, clean


def test_clean_uppercases_and_strips_punctuation() -> None:
    assert clean("General Electric Co., N.Y.") == "GENERAL ELECTRIC CO N Y"


def test_resolve_creates_then_fuzzy_matches() -> None:
    memory = EntityMemory()
    canonical, is_new, _score = memory.resolve("GENERAL ELECTRIC COMPANY", threshold=90)
    assert is_new
    assert canonical == "GENERAL ELECTRIC COMPANY"

    # a near-duplicate resolves to the same canonical (not a new entity)
    again, is_new2, _score2 = memory.resolve("GENERAL ELECTRIC COMPANY, A CORP", threshold=80)
    assert not is_new2
    assert again == "GENERAL ELECTRIC COMPANY"


def test_resolve_exact_alias_is_memoized() -> None:
    memory = EntityMemory(canonicals=["ACME CORP"])
    memory.resolve("acme corp!", threshold=90)  # learns alias
    assert clean("acme corp!") in memory.aliases


def test_seed_from_csv_alias_and_canonical(tmp_path: Path) -> None:
    csv_path = tmp_path / "seed.csv"
    csv_path.write_text("GE,GENERAL ELECTRIC\nACME LLC,ACME\n", encoding="utf-8")
    memory = EntityMemory()
    memory.seed_from_file(csv_path)
    assert "GENERAL ELECTRIC" in memory.canonicals
    assert memory.resolve("ge", threshold=90)[0] == "GENERAL ELECTRIC"  # via alias


def test_seed_from_json_and_roundtrip(tmp_path: Path) -> None:
    memory = EntityMemory(canonicals=["WIDGET CORP"])
    memory.resolve("WIDGET CORP INC", threshold=80)  # learn an alias
    path = tmp_path / "entities.json"
    memory.save(path)

    reloaded = EntityMemory.load(path)
    assert "WIDGET CORP" in reloaded.canonicals
    assert reloaded.resolve("widget corp inc", threshold=80)[0] == "WIDGET CORP"


def test_merge_and_apply_learned() -> None:
    a = EntityMemory(canonicals=["ALPHA"])
    b = EntityMemory(canonicals=["BETA"])
    a.merge(b)
    assert {"ALPHA", "BETA"} <= set(a.canonicals)

    c = EntityMemory()
    c.apply_learned([("gamma key", "GAMMA", 100)])
    assert "GAMMA" in c.canonicals


def test_resolve_match_only_does_not_create_new_canonical() -> None:
    memory = EntityMemory(canonicals=["ACME CORP"])
    before = memory.counts()[0]
    canonical, is_new, _score = memory.resolve("TOTALLY DIFFERENT CO", threshold=90, learn=False)
    assert canonical == "TOTALLY DIFFERENT CO"  # returned as-is
    assert not is_new
    assert memory.counts()[0] == before  # nothing added — curated memory stays clean


def test_resolve_uses_prefix_block_for_matching() -> None:
    memory = EntityMemory(canonicals=["GENERAL ELECTRIC COMPANY", "GENUINE PARTS COMPANY"])
    # a GEN-prefixed variant matches within its block
    assert memory.resolve("GENERAL ELECTRIC CO", threshold=80)[0] == "GENERAL ELECTRIC COMPANY"


def test_resolve_with_token_set_scorer() -> None:
    memory = EntityMemory(canonicals=["GENERAL ELECTRIC COMPANY"])
    # token_set ignores extra tokens — a strong match despite the trailing legal boilerplate
    # (the shared prefix keeps it in the same block, which blocking requires).
    canonical, _, _ = memory.resolve(
        "GENERAL ELECTRIC COMPANY A NEW YORK CORPORATION", threshold=80, scorer="token_set"
    )
    assert canonical == "GENERAL ELECTRIC COMPANY"


def test_match_returns_canonical_or_none_without_mutating() -> None:
    memory = EntityMemory(canonicals=["ADOBE SYSTEMS INCORPORATED"])
    assert (
        memory.match("Adobe Systems, Inc.")[0] == "ADOBE SYSTEMS INCORPORATED"  # type: ignore[index]
    )  # cleaned exact/fuzzy
    assert memory.match("TOTALLY UNKNOWN CO") is None  # no match -> None (distinguishable)
    assert memory.counts() == (1, 0)  # pure: nothing learned or added


def test_rename_canonical_repoints_aliases() -> None:
    memory = EntityMemory(canonicals=["ACME CORP"])
    memory.resolve("acme corp!", threshold=85)  # learn an alias -> ACME CORP
    memory.rename_canonical("ACME CORP", "ACME CORPORATION")
    assert "ACME CORPORATION" in memory.canonicals
    assert "ACME CORP" not in memory.canonicals
    assert memory.resolve("acme corp!", threshold=85)[0] == "ACME CORPORATION"  # alias followed


def test_delete_canonical_removes_it_and_its_aliases() -> None:
    memory = EntityMemory(canonicals=["ACME CORP", "BETA INC"])
    memory.resolve("acme corp!", threshold=85)
    memory.delete_canonical("ACME CORP")
    assert "ACME CORP" not in memory.canonicals
    assert clean("acme corp!") not in memory.aliases  # its alias is purged too
    # the block index was rebuilt, so remaining canonicals still resolve
    assert memory.resolve("BETA INC", threshold=90)[0] == "BETA INC"


def test_merge_canonicals_folds_source_into_target() -> None:
    memory = EntityMemory(canonicals=["ACME CORP", "ACME CORPORATION"])
    memory.resolve("acme corp llc", threshold=85)  # learns an alias to one of them
    memory.merge_canonicals("ACME CORP", "ACME CORPORATION")
    assert "ACME CORP" not in memory.canonicals
    assert memory.resolve("ACME CORP", threshold=95)[0] == "ACME CORPORATION"  # old key repointed


def test_set_and_delete_alias() -> None:
    memory = EntityMemory(canonicals=["ACME CORPORATION"])
    memory.set_alias("acme", "ACME CORPORATION")  # key is cleaned to "ACME"
    assert "ACME" in memory.aliases
    assert memory.resolve("ACME", threshold=100)[0] == "ACME CORPORATION"  # exact alias hit
    memory.delete_alias("acme")
    assert "ACME" not in memory.aliases


def test_normalize_column_splits_concatenated_values() -> None:
    memory = EntityMemory(canonicals=["ACME CORP", "BETA INC"])
    table = pa.table({"names": ["ACME CORP; BETA INC", "acme corp!", None]})
    result = normalize_column(
        table, "names", "names_canonical", memory, threshold=85, separator="; "
    )
    canon = result.column("names_canonical").to_pylist()
    assert canon[0] == "ACME CORP; BETA INC"  # each part normalized then rejoined
    assert canon[1] == "ACME CORP"
    assert canon[2] is None


def test_normalize_column_adds_canonical_and_reports_progress() -> None:
    table = pa.table({"name": ["ACME CORP", "acme corp", "BETA INC", None, "ACME CORP, LLC"]})
    memory = EntityMemory()
    progress: list[tuple[int, int]] = []
    result = normalize_column(
        table,
        "name",
        "name_canonical",
        memory,
        threshold=85,
        on_progress=lambda d, t: progress.append((d, t)),
    )
    canon = result.column("name_canonical").to_pylist()
    # all ACME variants collapse to one canonical; null stays null
    assert canon[0] == canon[1] == canon[4]
    assert canon[3] is None
    assert canon[2] != canon[0]
    assert progress and progress[-1][0] == progress[-1][1]  # final (total, total) reported


def test_shared_blocking_caps_pathological_prefix() -> None:
    """Regression: a gazetteer with tens of thousands of orgs behind one 4-char prefix must not
    degrade the shared EntityMemory fuzzy block to a full scan (the Item-1 cap, now shared by
    normalize + reference, not only the ledger cascade)."""
    names = [f"THE {i:06d} HOLDINGS LLC" for i in range(50_000)]  # all share "THE "
    memory = EntityMemory(canonicals=names)
    assert memory.max_block() <= _BLOCK_CAP  # bounded, not 50k
    # a match still resolves the right entity within its (re-split) sub-block
    matched = memory.match("THE 000042 HOLDINGS LLC", threshold=90)
    assert matched is not None and matched[0] == "THE 000042 HOLDINGS LLC"

    # the shared index re-splits deterministically and keeps identical keys together
    diverging: CappedBlockIndex[str] = CappedBlockIndex(
        [f"ACME {i:05d}" for i in range(5000)], cap=500
    )
    assert diverging.max_block() <= 500
    clones: CappedBlockIndex[str] = CappedBlockIndex(["IDENTICAL NAME"] * 40, cap=10)
    assert len(clones.candidates("IDENTICAL NAME")) == 40  # kept whole past the max prefix


def test_blocking_results_unchanged_below_cap() -> None:
    """Below the cap the shared index behaves like plain prefix blocking (no regressions)."""
    memory = EntityMemory(canonicals=["ACME CORPORATION", "ACME CORP", "BETA LLC"])
    close = memory.match("ACME CORPORATON", threshold=85)
    assert close is not None and close[0] in {"ACME CORPORATION", "ACME CORP"}
    assert memory.match("ZETA INC", threshold=90) is None  # different prefix block → no match


def test_resolve_reports_score_and_alias_provenance() -> None:
    memory = EntityMemory(canonicals=["ACME CORPORATION"])
    canonical, is_new, score = memory.resolve("ACME CORPORATON", threshold=85)  # typo -> fuzzy
    assert canonical == "ACME CORPORATION"
    assert not is_new
    assert 85 <= score < 100
    # the exact alias hit on a later run reports the ORIGINAL learn score, not 100
    again, _, score2 = memory.resolve("ACME CORPORATON", threshold=85)
    assert again == "ACME CORPORATION"
    assert score2 == score
    assert memory.alias_score(clean("ACME CORPORATON")) == score
    # a brand-new name is its own canonical: identity mapping, score 100
    _, is_new3, score3 = memory.resolve("ZINGWHAT WIDGETS LLC", threshold=90)
    assert is_new3 and score3 == 100
    # match-only miss: score 0
    fresh = EntityMemory(canonicals=["ACME CORPORATION"])
    _, _, score4 = fresh.resolve("TOTALLY DIFFERENT", threshold=95, learn=False)
    assert score4 == 0


def test_entities_json_v2_roundtrip_and_v1_legacy_load(tmp_path: Path) -> None:
    memory = EntityMemory(canonicals=["ACME CORPORATION"])
    memory.resolve("ACME CORPORATON", threshold=85)  # fuzzy-learned alias with score < 100
    path = tmp_path / "entities.json"
    memory.save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 3  # current schema; alias [canonical, score] format unchanged from v2
    fuzzy_values: list[list[Any]] = [
        cast("list[Any]", v) for v in raw["aliases"].values() if isinstance(v, list)
    ]
    assert fuzzy_values and fuzzy_values[0][0] == "ACME CORPORATION" and fuzzy_values[0][1] < 100

    reloaded = EntityMemory.load(path)
    assert reloaded.alias_score(clean("ACME CORPORATON")) < 100  # score survives the roundtrip

    # v1 legacy file (bare string values) loads with score 100
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(
            {"canonicals": ["ACME CORPORATION"], "aliases": {"acme corp": "ACME CORPORATION"}}
        ),
        encoding="utf-8",
    )
    old = EntityMemory.load(legacy)
    assert old.aliases == {"acme corp": "ACME CORPORATION"}
    assert old.alias_score("acme corp") == 100


def test_entity_type_set_get_and_v3_roundtrip(tmp_path: Path) -> None:
    memory = EntityMemory(canonicals=["ACME CORPORATION", "SMITH, JOHN"])
    memory.set_type("ACME CORPORATION", "company")
    memory.set_type("SMITH, JOHN", "individual")
    memory.set_type("NOT A CANONICAL", "company")  # ignored: not a canonical
    assert memory.entity_type("ACME CORPORATION") == "company"
    assert memory.entity_type("SMITH, JOHN") == "individual"
    assert memory.entity_type("NOT A CANONICAL") is None
    assert memory.types == {"ACME CORPORATION": "company", "SMITH, JOHN": "individual"}

    path = tmp_path / "entities.json"
    memory.save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 3
    assert raw["types"] == {"ACME CORPORATION": "company", "SMITH, JOHN": "individual"}
    reloaded = EntityMemory.load(path)
    assert reloaded.types == memory.types


def test_v2_file_without_types_loads_with_empty_types(tmp_path: Path) -> None:
    legacy = tmp_path / "v2.json"
    legacy.write_text(
        json.dumps({"version": 2, "canonicals": ["ACME CORPORATION"], "aliases": {}}),
        encoding="utf-8",
    )
    memory = EntityMemory.load(legacy)
    assert memory.types == {}
    assert memory.entity_type("ACME CORPORATION") is None


def test_entity_type_stays_in_sync_with_canonical_edits() -> None:
    memory = EntityMemory(canonicals=["ACME CORP", "BETA INC"])
    memory.set_type("ACME CORP", "company")
    memory.set_type("BETA INC", "company")

    memory.rename_canonical("ACME CORP", "ACME CORPORATION")  # tag follows the rename
    assert memory.entity_type("ACME CORPORATION") == "company"
    assert memory.entity_type("ACME CORP") is None

    memory.delete_canonical("BETA INC")  # tag dropped with the canonical
    assert memory.entity_type("BETA INC") is None

    memory.add_canonical("GAMMA LLC")
    memory.merge_canonicals("ACME CORPORATION", "GAMMA LLC")  # source tag moves to untagged target
    assert memory.entity_type("GAMMA LLC") == "company"
    assert memory.entity_type("ACME CORPORATION") is None


def test_merge_absorbs_other_memory_types() -> None:
    a = EntityMemory(canonicals=["ACME CORP"])
    b = EntityMemory(canonicals=["BETA INC"])
    b.set_type("BETA INC", "company")
    a.merge(b)
    assert a.entity_type("BETA INC") == "company"


def test_normalize_column_emits_entity_type_column() -> None:
    memory = EntityMemory(canonicals=["ACME CORPORATION", "SMITH, JOHN"])
    memory.set_type("ACME CORPORATION", "company")
    memory.set_type("SMITH, JOHN", "individual")
    table = pa.table(
        {"buyer": ["ACME CORPORATION", "SMITH, JOHN", "ACME CORPORATION; SMITH, JOHN", None]}
    )
    result = normalize_column(
        table,
        "buyer",
        "buyer_canonical",
        memory,
        separator="; ",
        learn=False,
        type_target="buyer_canonical_type",
    )
    kinds = result.column("buyer_canonical_type").to_pylist()
    assert kinds[0] == "company"
    assert kinds[1] == "individual"
    assert kinds[2] == "unknown"  # multi-party parties disagree
    assert kinds[3] is None  # null passthrough


def test_normalize_column_emits_score_and_review_columns() -> None:
    memory = EntityMemory(canonicals=["ACME CORPORATION", "GLOBEX LLC"])
    table = pa.table({"name": ["ACME CORPORATION", "ACME CORPORATON", "NEWCO VENTURES", None]})
    result = normalize_column(
        table,
        "name",
        "name_canonical",
        memory,
        threshold=85,
        score_target="name_canonical_score",
        review_target="name_canonical_review",
        review_threshold=98,
    )
    scores = result.column("name_canonical_score").to_pylist()
    review = result.column("name_canonical_review").to_pylist()
    assert scores[0] == 100  # identical to its canonical
    assert scores[1] is not None and 85 <= scores[1] < 98  # fuzzy typo -> review band
    assert review[1] == "true"
    assert scores[2] == 100 and review[2] == "false"  # new canonical: identity, not review
    assert scores[3] is None and review[3] is None  # null passthrough
