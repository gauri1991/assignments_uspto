"""Tests for the CLI entry point (uspto_assignments.cli)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pyarrow.parquet as pq
import pytest

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


def test_cli_build_reference_auto_detects(tmp_path: Path) -> None:
    raw = tmp_path / "g_assignee_disambiguated.tsv"
    raw.write_text(
        "disambig_assignee_organization\tassignee_id\n"
        "ADOBE SYSTEMS INCORPORATED\tA1\n"
        "QUALCOMM INCORPORATED\tA4\n",
        encoding="utf-8",
    )
    out = tmp_path / "reference.parquet"
    main(["build-reference", str(raw), "--out", str(out)])  # no column flags: auto-detected
    assert set(pq.read_schema(out).names) == {"organization", "assignee_id"}  # pyright: ignore[reportUnknownMemberType]


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


def test_cli_manifest_paths_bare_with_relative_outdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a relative --outdir must not prefix manifest paths with the dir itself."""
    monkeypatch.chdir(tmp_path)
    main(["parse", str(FIXTURE), "--outdir", "out"])
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    for output in manifest["outputs"]:
        assert "/" not in output["path"]  # bare filename, resolvable relative to the manifest
        assert (tmp_path / "out" / output["path"]).is_file()


def test_cli_ingest_removes_its_temp_work_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: ingest of a file must not leave the intermediate Arrow store in /tmp."""
    created: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def tracking_mkdtemp(prefix: str) -> str:
        path = real_mkdtemp(prefix=prefix, dir=str(tmp_path))
        created.append(Path(path))
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", tracking_mkdtemp)
    main(["ingest", str(FIXTURE), "--out", str(tmp_path / "raw")])
    assert len(created) == 1
    assert not created[0].exists()
