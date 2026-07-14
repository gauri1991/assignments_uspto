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


def test_templates_summary_writes_markdown(tmp_path: Path) -> None:
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "demo.json").write_text(
        json.dumps(
            [
                {
                    "name": "demo",
                    "load": {"limit": None},
                    "steps": [
                        {
                            "kind": "filter",
                            "table": "flat",
                            "clauses": [
                                {
                                    "column": "nope_col",  # deliberate warning
                                    "op": "contains",
                                    "value": "x",
                                    "value2": "",
                                    "case_sensitive": False,
                                }
                            ],
                            "combine": "and",
                        },
                        {"kind": "export", "fmt": "csv", "tables": ["flat"]},
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    out = tmp_path / "SUMMARY.md"
    main(["templates-summary", "--templates", str(templates_dir), "--out", str(out)])
    text = out.read_text(encoding="utf-8")
    assert "### demo" in text
    assert "1. Filter · flat · 1 clause(s) · AND" in text
    assert "⚠" in text and "nope_col" in text
