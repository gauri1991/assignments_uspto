"""Tests for the buyer pipeline: conveyance, propid, dictionary, resolution, ledger."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow.parquet as _pq

from uspto_assignments import (
    CappedBlockIndex,
    build_dictionary,
    build_ledger,
    classify_conveyance,
    doc_type_for,
    load_dictionary,
    normalize_patent_id,
    reconcile_cpc,
    resolve_mentions,
    top_buyers,
)
from uspto_assignments.cli import main as cli_main
from uspto_assignments.dictionary import append_provisionals, strip_legal_suffix
from uspto_assignments.ledger import BRIDGE_FILE, BUYERS_FILE, LEDGER_FILE

# pyarrow.parquet is under-typed in pyarrow-stubs; route through Any (see filters.py).
pq: Any = _pq

FIXTURES = Path(__file__).parent / "fixtures"
BUYER_XML = FIXTURES / "buyer_sample.xml"
SEED_TSV = FIXTURES / "seed_gazetteer.tsv"


# -- conveyance taxonomy ------------------------------------------------------


def test_conveyance_taxonomy_first_match_wins() -> None:
    cases = {
        "ASSIGNMENT OF ASSIGNORS INTEREST (SEE DOCUMENT FOR DETAILS).": "assignment",
        "ASSIGNMENT OF ASSIGNOR'S INTEREST": "assignment",
        "NUNC PRO TUNC ASSIGNMENT (SEE DOCUMENT FOR DETAILS).": "nunc_pro_tunc",
        "CORRECTIVE ASSIGNMENT TO CORRECT THE RECEIVING PARTY": "correction",
        "SECURITY INTEREST (SEE DOCUMENT FOR DETAILS).": "security_interest",
        "ASSIGNMENT FOR SECURITY": "security_interest",
        "RELEASE BY SECURED PARTY": "release",
        "MERGER (SEE DOCUMENT FOR DETAILS).": "merger",
        "CHANGE OF NAME": "name_change",
        "LICENSE (SEE DOCUMENT FOR DETAILS).": "license",
        "": "other",
    }
    for text, expected in cases.items():
        assert classify_conveyance(text) == expected, text


# -- property identity --------------------------------------------------------


def test_doc_type_from_real_kind_vocabulary() -> None:
    assert doc_type_for("16123456", "X0") == "application"
    assert doc_type_for("10987654", "B2") == "grant"
    assert doc_type_for("4367069", "B1") == "grant"
    assert doc_type_for("20210012345", "A1") == "publication"
    assert doc_type_for("4147078", "A") == "grant"  # legacy pre-2001 grant kind
    assert doc_type_for("D0912345", "") == "grant"  # design grant by number prefix
    assert doc_type_for("PCT/US2005/012345", "") == "unknown"


def test_patent_id_normalized_patentsview_convention() -> None:
    assert normalize_patent_id("10987654", "grant") == "10987654"
    assert normalize_patent_id("US06123456", "grant") == "6123456"  # US prefix + zeros stripped
    assert normalize_patent_id("D0912345", "grant") == "D912345"
    assert normalize_patent_id("RE034567", "grant") == "RE34567"
    assert normalize_patent_id("16123456", "application") == ""  # non-grants stay empty
    assert normalize_patent_id("10987654", "grant", fmt="raw") == "10987654"


# -- dictionary artifact ------------------------------------------------------


def test_build_and_load_dictionary(tmp_path: Path) -> None:
    manifest = build_dictionary(tmp_path / "dict", patentsview=SEED_TSV)
    assert manifest["entities"] == 3  # the individual row has an empty org -> excluded
    dictionary = load_dictionary(tmp_path / "dict")
    assert dictionary.lookup_exact("WIDGET CORP") == "pv-widget"
    # the suffix-stripped alias resolves legal-form variants exactly
    assert dictionary.lookup_exact("GADGET HOLDINGS") == "pv-gadget"
    assert (tmp_path / "dict" / "manifest.json").is_file()
    manifest_data = json.loads((tmp_path / "dict" / "manifest.json").read_text())
    assert manifest_data["sources"][0]["sha256"]


def test_provisionals_persist_and_stay_stable(tmp_path: Path) -> None:
    build_dictionary(tmp_path / "dict", patentsview=SEED_TSV)
    dictionary = load_dictionary(tmp_path / "dict")
    resolved, new = resolve_mentions(["QUANTUM FLUX VENTURES LLC"], dictionary)
    first_id = resolved["QUANTUM FLUX VENTURES LLC"].entity_id
    assert first_id.startswith("prov-") and new
    append_provisionals(tmp_path / "dict", new)
    # second run: same mention now resolves by EXACT lookup to the SAME id
    dictionary2 = load_dictionary(tmp_path / "dict")
    resolved2, new2 = resolve_mentions(["QUANTUM FLUX VENTURES LLC"], dictionary2)
    again = resolved2["QUANTUM FLUX VENTURES LLC"]
    assert again.entity_id == first_id and again.resolution_source == "exact"
    assert not new2  # nothing new to mint


# -- resolution cascade -------------------------------------------------------


def test_resolution_cascade_sources(tmp_path: Path) -> None:
    build_dictionary(tmp_path / "dict", patentsview=SEED_TSV)
    dictionary = load_dictionary(tmp_path / "dict")
    resolved, _ = resolve_mentions(
        ["WIDGET CORP", "WIDGET CORP.", "Widgett Corp", "SMITH, JOHN", "XYZZY FROBNICATORS LLC"],
        dictionary,
    )
    assert resolved["WIDGET CORP"].resolution_source == "exact"
    assert resolved["WIDGET CORP."].resolution_source == "exact"  # cleaning removes punctuation
    fuzzy = resolved["Widgett Corp"]
    assert fuzzy.resolution_source == "fuzzy" and fuzzy.entity_id == "pv-widget"
    assert 0.0 < fuzzy.resolution_confidence <= 1.0
    person = resolved["SMITH, JOHN"]
    assert person.entity_type == "individual" and person.resolution_source == "person"
    provisional = resolved["XYZZY FROBNICATORS LLC"]
    assert provisional.resolution_source == "provisional"
    assert provisional.entity_id.startswith("prov-")
    assert provisional.entity_type == "company"  # LLC keyword classifies as company


def test_provisional_clustering_collapses_spelling_variants(tmp_path: Path) -> None:
    build_dictionary(tmp_path / "dict", patentsview=SEED_TSV)
    dictionary = load_dictionary(tmp_path / "dict")
    resolved, _ = resolve_mentions(
        ["ZORPTECH SYSTEMS INC", "ZORPTECH SYSTEMS INCORPORATED"], dictionary
    )
    a = resolved["ZORPTECH SYSTEMS INC"]
    b = resolved["ZORPTECH SYSTEMS INCORPORATED"]
    assert a.entity_id == b.entity_id  # off-gazetteer variants collapse to one buyer


def test_capped_block_index_bounds_pathological_prefixes() -> None:
    # 5,000 keys sharing one 4-char prefix (`'THE '`) — the shape that degraded the old blocking —
    # but diverging afterwards, so the index re-splits them onto longer prefixes.
    keys = [f"THE {i:05d} COMPANY" for i in range(5000)]
    index: CappedBlockIndex[str] = CappedBlockIndex(keys, cap=500)
    assert index.max_block() <= 500  # every stored block respects the cap
    block = index.candidates("THE 00001 COMPANY")
    assert 0 < len(block) <= 500  # probe hits a re-split sub-block, not a full prefix scan
    # keys identical beyond the max prefix length are kept whole (accepted, never lost)
    clones: CappedBlockIndex[str] = CappedBlockIndex(["SAME PREFIX FOREVER X"] * 50, cap=10)
    assert len(clones.candidates("SAME PREFIX FOREVER X")) == 50


def test_strip_legal_suffix() -> None:
    assert strip_legal_suffix("ACME CORP INC") == "ACME"
    assert strip_legal_suffix("GADGET HOLDINGS LLC") == "GADGET HOLDINGS"
    assert strip_legal_suffix("PLAIN NAME") == "PLAIN NAME"


# -- end-to-end ledger on the fixture ------------------------------------------


def _run_pipeline(tmp_path: Path) -> Path:
    raw = tmp_path / "raw"
    cli_main(["ingest", str(BUYER_XML), "--out", str(raw)])
    cli_main(["build-dictionary", "--patentsview", str(SEED_TSV), "--out", str(tmp_path / "dict")])
    out = tmp_path / "ledger"
    build_ledger(raw, tmp_path / "dict", out, kept_types=frozenset({"assignment"}))
    return out


def test_ledger_firm_to_firm_predicate_and_contracts(tmp_path: Path) -> None:
    out = _run_pipeline(tmp_path)
    ledger = pq.read_table(out / LEDGER_FILE)
    # of the 5 fixture records: only #2 survives — #1 sellers are persons, #3 is an intra-group
    # self-transfer (same provisional cluster), #4 is a security interest, #5 is nunc-pro-tunc
    assert ledger.num_rows == 1
    row = ledger.to_pylist()[0]
    assert (row["reel_no"], row["frame_no"]) == ("100002", "0002")
    assert row["transaction_date"] == "20240215"  # LATEST signer execution date
    assert row["date_source"] == "execution"
    assert row["conveyance_type"] == "assignment"
    assert row["buyer_entity_ids"] == ["pv-gadget"]
    assert set(row["seller_entity_ids"]) == {"pv-widget"}  # both spellings resolve to one entity
    assert row["property_count"] == 2  # app+grant+pub collapse to 1 property, + 1 grant-only

    buyers = pq.read_table(out / BUYERS_FILE)
    assert buyers.num_rows == 1
    buyer = buyers.to_pylist()[0]
    assert buyer["entity_id"] == "pv-gadget"
    assert buyer["deals_count"] == 1 and buyer["patents_count"] == 2
    assert buyer["resolution_source"] == "exact" and buyer["is_off_gazetteer"] is False
    assert buyer["first_acquisition_date"] == "20240215"

    bridge = pq.read_table(out / BRIDGE_FILE)
    rows = bridge.to_pylist()
    assert {r["canonical_property_id"] for r in rows} == {"16123456", "11222333"}
    grant_rows = [r for r in rows if r["doc_type"] == "grant"]
    assert {r["patent_id_normalized"] for r in grant_rows} == {"10987654", "11222333"}
    assert all(r["cpc_lookup_status"] == "" for r in rows)  # empty until a CPC join
    metrics = json.loads((out / "metrics.json").read_text())
    assert metrics["transactions_firm_to_firm"] == 1


def test_nunc_pro_tunc_dial_includes_off_gazetteer_buyer(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    cli_main(["ingest", str(BUYER_XML), "--out", str(raw)])
    cli_main(["build-dictionary", "--patentsview", str(SEED_TSV), "--out", str(tmp_path / "dict")])
    out = tmp_path / "ledger"
    build_ledger(raw, tmp_path / "dict", out, kept_types=frozenset({"assignment", "nunc_pro_tunc"}))
    buyers = pq.read_table(out / BUYERS_FILE).to_pylist()
    by_id = {b["entity_id"]: b for b in buyers}
    provisional = [b for b in buyers if b["is_off_gazetteer"]]
    assert len(provisional) == 1  # QUANTUM FLUX VENTURES LLC — resolved, counted, and flagged
    assert provisional[0]["entity_id"].startswith("prov-")
    assert "pv-gadget" in by_id  # gazetteer-confirmed buyer still present


def test_reconcile_cpc_attaches_codes_and_hit_rate(tmp_path: Path) -> None:
    out = _run_pipeline(tmp_path)
    cpc = tmp_path / "cpc.csv"
    # patent 10987654 has two CPC symbols across two subclasses; 11222333 is absent (not_found).
    cpc.write_text(
        "patent_id,cpc_group\n10987654,H01L21/02\n10987654,G06F3/01\n",
        encoding="utf-8",
    )
    metrics = reconcile_cpc(out, cpc)
    assert metrics["cpc_eligible_rows"] == 2  # the two grant rows; app rows are 'na'
    assert metrics["cpc_found"] == 1
    assert metrics["cpc_hit_rate"] == 0.5
    rows = pq.read_table(out / BRIDGE_FILE).to_pylist()
    by_status = {(r["doc_type"], r["cpc_lookup_status"]) for r in rows}
    assert ("application", "na") in by_status
    assert ("grant", "found") in by_status and ("grant", "not_found") in by_status
    # the actual CPC CODES are attached per patent (the downstream CPC-matching feed)
    found_row = next(r for r in rows if r["cpc_lookup_status"] == "found")
    assert sorted(found_row["cpc_codes"]) == ["G06F3/01", "H01L21/02"]
    assert sorted(found_row["cpc_subclasses"]) == ["G06F", "H01L"]  # 4-char grain
    na_row = next(r for r in rows if r["cpc_lookup_status"] == "na")
    assert na_row["cpc_codes"] == []  # non-grant rows carry empty lists, not nulls
    # re-running is idempotent (no duplicate cpc_* columns, same result)
    metrics2 = reconcile_cpc(out, cpc)
    assert metrics2 == metrics


def test_top_buyers_leaderboard_and_sampled_mode(tmp_path: Path) -> None:
    out = _run_pipeline(tmp_path)
    board = top_buyers(out, by="patents", top=5)
    assert board.num_rows == 1 and board.to_pylist()[0]["patents_count"] == 2
    sampled = top_buyers(out, by="patents", top=5, cpc_mode="sampled", sample=1)
    assert sampled.to_pylist()[0]["patents_count"] == 1  # capped to 1 most recent grant


def test_ledger_is_deterministic_across_fresh_runs(tmp_path: Path) -> None:
    """Item 4: two independent fresh builds must produce identical buyer ids and clusters —
    provisional minting/clustering is order-independent (sorted inputs + content-hash ids)."""
    raw = tmp_path / "raw"
    cli_main(["ingest", str(BUYER_XML), "--out", str(raw)])
    signatures: list[list[tuple[str, str]]] = []
    for run in ("a", "b"):
        dict_dir = tmp_path / f"dict_{run}"  # a FRESH dictionary each run (no persisted carryover)
        cli_main(["build-dictionary", "--patentsview", str(SEED_TSV), "--out", str(dict_dir)])
        out = tmp_path / f"ledger_{run}"
        build_ledger(raw, dict_dir, out, kept_types=frozenset({"assignment", "nunc_pro_tunc"}))
        buyers = pq.read_table(out / BUYERS_FILE).to_pylist()
        signatures.append(sorted((b["entity_id"], b["canonical_name"]) for b in buyers))
    assert signatures[0] == signatures[1]  # identical ids AND clusters
    assert any(eid.startswith("prov-") for eid, _ in signatures[0])  # incl. off-gazetteer buyers


def test_cli_legacy_parse_still_works(tmp_path: Path) -> None:
    cli_main([str(BUYER_XML), "--outdir", str(tmp_path / "legacy"), "--formats", "parquet"])
    assert (tmp_path / "legacy" / "assignments.parquet").is_file()


def test_cli_resolve_writes_mentions(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    cli_main(["ingest", str(BUYER_XML), "--out", str(raw)])
    cli_main(["build-dictionary", "--patentsview", str(SEED_TSV), "--out", str(tmp_path / "dict")])
    cli_main(
        [
            "resolve",
            "--raw",
            str(raw),
            "--dict",
            str(tmp_path / "dict"),
            "--out",
            str(tmp_path / "resolved"),
        ]
    )
    mentions = pq.read_table(tmp_path / "resolved" / "mentions.parquet")
    assert mentions.num_rows > 0
    assert set(mentions.column_names) >= {
        "mention",
        "entity_id",
        "entity_type",
        "resolution_source",
        "resolution_confidence",
        "ultimate_parent_id",
    }
    # every row carries a source + confidence (the acceptance criterion)
    assert all(r["resolution_source"] for r in mentions.to_pylist())
