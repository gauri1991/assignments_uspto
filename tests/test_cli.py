"""Tests for the CLI entry point (uspto_assignments.cli)."""

from __future__ import annotations

import json
from pathlib import Path

from uspto_assignments.cli import main

FIXTURE = Path(__file__).parent / "fixtures" / "sample_assignment.xml"


def test_cli_parse_writes_manifest(tmp_path: Path) -> None:
    main(["parse", str(FIXTURE), "--outdir", str(tmp_path)])
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == 1
    assert manifest["command"] == "parse"
    assert manifest["input"].endswith("sample_assignment.xml")
    for output in manifest["outputs"]:
        assert (tmp_path / output["path"]).is_file()
        assert output["rows"] is None or output["rows"] >= 0


def test_cli_ingest_writes_manifest(tmp_path: Path) -> None:
    out = tmp_path / "raw"
    main(["ingest", str(FIXTURE), "--out", str(out)])
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["command"] == "ingest"
    paths = [entry["path"] for entry in manifest["outputs"]]
    assert "assignments.parquet" in paths
    for output in manifest["outputs"]:
        assert (out / output["path"]).is_file()
