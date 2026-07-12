"""Tests for entity-name normalization (uspto_assignments.normalize)."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa

from uspto_assignments import EntityMemory, normalize_column
from uspto_assignments.normalize import _BLOCK_CAP, CappedBlockIndex, clean


def test_clean_uppercases_and_strips_punctuation() -> None:
    assert clean("General Electric Co., N.Y.") == "GENERAL ELECTRIC CO N Y"


def test_resolve_creates_then_fuzzy_matches() -> None:
    memory = EntityMemory()
    canonical, is_new = memory.resolve("GENERAL ELECTRIC COMPANY", threshold=90)
    assert is_new
    assert canonical == "GENERAL ELECTRIC COMPANY"

    # a near-duplicate resolves to the same canonical (not a new entity)
    again, is_new2 = memory.resolve("GENERAL ELECTRIC COMPANY, A CORP", threshold=80)
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
    c.apply_learned([("gamma key", "GAMMA")])
    assert "GAMMA" in c.canonicals


def test_resolve_match_only_does_not_create_new_canonical() -> None:
    memory = EntityMemory(canonicals=["ACME CORP"])
    before = memory.counts()[0]
    canonical, is_new = memory.resolve("TOTALLY DIFFERENT CO", threshold=90, learn=False)
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
    canonical, _ = memory.resolve(
        "GENERAL ELECTRIC COMPANY A NEW YORK CORPORATION", threshold=80, scorer="token_set"
    )
    assert canonical == "GENERAL ELECTRIC COMPANY"


def test_match_returns_canonical_or_none_without_mutating() -> None:
    memory = EntityMemory(canonicals=["ADOBE SYSTEMS INCORPORATED"])
    assert (
        memory.match("Adobe Systems, Inc.") == "ADOBE SYSTEMS INCORPORATED"
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
    assert memory.match("THE 000042 HOLDINGS LLC", threshold=90) == "THE 000042 HOLDINGS LLC"

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
    assert memory.match("ACME CORPORATON", threshold=85) in {"ACME CORPORATION", "ACME CORP"}
    assert memory.match("ZETA INC", threshold=90) is None  # different prefix block → no match
