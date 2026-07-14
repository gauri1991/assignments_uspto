"""Command-line entry point: parse/convert plus the buyer-identification pipeline subcommands.

Subcommands::

    uspto-assign parse <xml|zip> …               # legacy conversion (bare paths still work)
    uspto-assign ingest <xml|zip|dataset-dir> --out data/raw   # land Parquet, natural grain
    uspto-assign build-dictionary --patentsview reference/…tsv --out dictionary
    uspto-assign resolve --raw data/raw --dict dictionary --out data/resolved
    uspto-assign ledger  --raw data/raw --dict dictionary --out data/ledger
    uspto-assign report  --ledger data/ledger [--by patents|deals] [--top 20]
                         [--cpc-mode sampled|full] [--cpc-file cpc.tsv]

The pipeline never touches the network: seed data is staged locally and baked into the dictionary
artifact at build time; every later stage only reads local Parquet.
"""

from __future__ import annotations

import argparse
import json
import logging
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as _pq

from .batch import describe_step, load_templates, validate_template
from .dictionary import build_dictionary
from .exporters import export_store
from .ledger import build_ledger, load_raw, reconcile_cpc, resolve_raw_mentions, top_buyers
from .parser import iter_records
from .tables import open_dataset, parse_to_store
from .writers import DEFAULT_BATCH_SIZE, write_excel, write_parquet

# pyarrow.parquet is under-typed in pyarrow-stubs; route through Any (see filters.py).
pq: Any = _pq

_SUBCOMMANDS = {
    "parse",
    "ingest",
    "build-dictionary",
    "resolve",
    "ledger",
    "report",
    "templates-summary",
}


def _parse_formats(raw: str) -> list[str]:
    valid = {"parquet", "excel"}
    chosen = [f.strip().lower() for f in raw.split(",") if f.strip()]
    invalid = [f for f in chosen if f not in valid]
    if invalid:
        raise argparse.ArgumentTypeError(
            f"unknown format(s): {', '.join(invalid)} (use parquet,excel)"
        )
    if not chosen:
        raise argparse.ArgumentTypeError("no formats selected")
    return chosen


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uspto-assign",
        description="USPTO patent-assignment parsing and buyer-identification pipeline.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("parse", help="parse XML/ZIP into Parquet + Excel (legacy default)")
    p.add_argument("input", type=Path, help="path to the USPTO assignment .xml or .zip file")
    p.add_argument("--outdir", type=Path, default=Path("out"), help="output directory")
    p.add_argument("--formats", type=_parse_formats, default=["parquet", "excel"])
    p.add_argument("--basename", default="assignments", help="Excel workbook filename stem")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)

    p = sub.add_parser("ingest", help="land the four raw tables as Parquet at natural grain")
    p.add_argument("input", type=Path, help=".xml/.zip file or an existing dataset folder")
    p.add_argument("--out", type=Path, required=True, help="output directory for raw Parquet")
    p.add_argument("--limit", type=int, default=None, help="cap parsed assignment records")

    p = sub.add_parser("build-dictionary", help="build the standalone resolution dictionary")
    p.add_argument("--out", type=Path, default=Path("dictionary"), help="artifact directory")
    p.add_argument("--patentsview", type=Path, help="PatentsView g_assignee_disambiguated file")
    p.add_argument("--name-column", default="disambig_assignee_organization")
    p.add_argument("--id-column", default="assignee_id")
    p.add_argument(
        "--extra",
        action="append",
        default=[],
        metavar="PATH:NAME_COL:ID_COL:SOURCE",
        help="additional seed source (generic adapter; repeatable)",
    )

    p = sub.add_parser("resolve", help="resolve all party mentions; write the resolution table")
    p.add_argument("--raw", type=Path, required=True, help="raw Parquet dir (from ingest)")
    p.add_argument("--dict", type=Path, required=True, dest="dict_dir")
    p.add_argument("--out", type=Path, required=True, help="output dir for mentions.parquet")
    p.add_argument("--threshold", type=int, default=92)
    p.add_argument("--scorer", default="token_sort")

    p = sub.add_parser("ledger", help="build transaction_ledger + buyers + buyer_property_bridge")
    p.add_argument("--raw", type=Path, required=True)
    p.add_argument("--dict", type=Path, required=True, dest="dict_dir")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--threshold", type=int, default=92)
    p.add_argument("--scorer", default="token_sort")
    p.add_argument(
        "--include-types",
        default="assignment",
        help="comma-separated conveyance types to keep (default: assignment)",
    )
    p.add_argument("--patent-id-format", choices=["patentsview", "raw"], default="patentsview")

    p = sub.add_parser("report", help="leaderboards + optional CPC reconciliation over the ledger")
    p.add_argument("--ledger", type=Path, required=True, dest="ledger_dir")
    p.add_argument("--by", choices=["patents", "deals"], default="patents")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--cpc-mode", choices=["full", "sampled"], default="full")
    p.add_argument("--sample", type=int, default=25, help="grants per buyer in sampled mode")
    p.add_argument("--cpc-file", type=Path, help="CPC table to reconcile against (csv/tsv/parquet)")
    p.add_argument("--cpc-patent-column", default="patent_id")
    p.add_argument(
        "--cpc-code-column",
        default="cpc_group",
        help="CPC-symbol column in the source (PatentsView g_cpc_current: cpc_group)",
    )
    p = sub.add_parser(
        "templates-summary",
        help="generate a steps-summary markdown for every template (run from the project root "
        "so relative reference paths validate)",
    )
    p.add_argument("--templates", type=Path, default=Path("templates"), dest="templates_dir")
    p.add_argument(
        "--out", type=Path, default=None, help="output file (default: <templates>/TEMPLATES.md)"
    )

    return parser


def _write_cli_manifest(
    out_dir: Path,
    command: str,
    source: Path,
    outputs: list[tuple[Path, int | None]],
    started: float,
) -> None:
    """Write ``manifest.json`` into ``out_dir``: the audit record of one direct CLI conversion."""
    payload = {
        "schema": 1,
        "command": command,
        "input": str(source),
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "duration_seconds": round(time.monotonic() - started, 1),
        "outputs": [
            {
                "path": str(path.relative_to(out_dir)) if path.is_absolute() else str(path),
                "rows": rows,
            }
            for path, rows in outputs
        ],
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _cmd_parse(args: argparse.Namespace) -> None:
    input_path: Path = args.input
    if not input_path.is_file():
        raise SystemExit(f"input file not found: {input_path}")
    started = time.monotonic()
    outdir: Path = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    outputs: list[tuple[Path, int | None]] = []
    if "parquet" in args.formats:
        counts = write_parquet(iter_records(input_path), outdir, args.basename, args.batch_size)
        for name, n in counts.items():
            print(f"parquet {name}: {n} rows -> {outdir / f'{name}.parquet'}")
            outputs.append((outdir / f"{name}.parquet", n))
    if "excel" in args.formats:
        counts = write_excel(iter_records(input_path), outdir, args.basename)
        print(f"excel -> {outdir / f'{args.basename}.xlsx'}")
        for name, n in counts.items():
            print(f"  sheet {name}: {n} rows")
        outputs.append((outdir / f"{args.basename}.xlsx", sum(counts.values())))
    _write_cli_manifest(outdir, "parse", input_path, outputs, started)


def _cmd_ingest(args: argparse.Namespace) -> None:
    source: Path = args.input
    if source.is_dir():
        store = open_dataset(source)
    elif source.is_file():
        work = Path(tempfile.mkdtemp(prefix="uspto_ingest_"))
        store = parse_to_store(source, work, limit=args.limit)
    else:
        raise SystemExit(f"input not found: {source}")
    started = time.monotonic()
    written = export_store(store, args.out, "parquet")
    outputs: list[tuple[Path, int | None]] = []
    for name, rows in written.items():
        print(f"ingested -> {name} ({rows:,} rows)")
        outputs.append((args.out / f"{name}.parquet", rows))
    _write_cli_manifest(args.out, "ingest", source, outputs, started)


def _cmd_build_dictionary(args: argparse.Namespace) -> None:
    extra: list[tuple[Path, str, str, str]] = []
    for spec in args.extra:
        parts = str(spec).split(":")
        if len(parts) != 4:
            raise SystemExit(f"--extra must be PATH:NAME_COL:ID_COL:SOURCE, got {spec!r}")
        extra.append((Path(parts[0]), parts[1], parts[2], parts[3]))
    if args.patentsview is None and not extra:
        raise SystemExit("build-dictionary needs --patentsview and/or --extra sources")
    manifest = build_dictionary(
        args.out,
        patentsview=args.patentsview,
        patentsview_name_column=args.name_column,
        patentsview_id_column=args.id_column,
        extra_sources=extra,
    )
    print(f"dictionary built: {manifest['entities']:,} entities, {manifest['aliases']:,} aliases")
    print(f"manifest -> {args.out / 'manifest.json'}")


def _cmd_resolve(args: argparse.Namespace) -> None:
    raw = load_raw(args.raw)
    resolution = resolve_raw_mentions(
        raw, args.dict_dir, threshold=args.threshold, scorer=args.scorer, on_event=print
    )
    args.out.mkdir(parents=True, exist_ok=True)
    out = args.out / "mentions.parquet"
    pq.write_table(resolution, out)
    print(f"resolved mentions -> {out} ({resolution.num_rows:,} rows)")


def _cmd_ledger(args: argparse.Namespace) -> None:
    kept = frozenset(t.strip() for t in args.include_types.split(",") if t.strip())
    metrics = build_ledger(
        args.raw,
        args.dict_dir,
        args.out,
        kept_types=kept,
        threshold=args.threshold,
        scorer=args.scorer,
        patent_id_format=args.patent_id_format,
        on_event=print,
    )
    for key, value in metrics.items():
        print(f"  {key}: {value}")


def _cmd_report(args: argparse.Namespace) -> None:
    if args.cpc_file is not None:
        metrics = reconcile_cpc(
            args.ledger_dir,
            args.cpc_file,
            patent_column=args.cpc_patent_column,
            code_column=args.cpc_code_column,
        )
        print(
            f"cpc_hit_rate: {metrics['cpc_hit_rate']:.2%} "
            f"({metrics['cpc_found']:,} of {metrics['cpc_eligible_rows']:,} eligible)"
        )
        low_hit_rate = 0.5
        if metrics["cpc_eligible_rows"] and metrics["cpc_hit_rate"] < low_hit_rate:
            print(
                "⚠ LOW CPC HIT RATE — patent_id_normalized and the CPC source formats are "
                "likely misaligned; check --patent-id-format and --cpc-patent-column."
            )
    board = top_buyers(
        args.ledger_dir, by=args.by, top=args.top, cpc_mode=args.cpc_mode, sample=args.sample
    )
    rows: list[dict[str, Any]] = board.to_pylist()
    print(f"\nTop buyers by {args.by} (cpc-mode={args.cpc_mode}):")
    for row in rows:
        flag = " [provisional]" if row.get("is_off_gazetteer") else ""
        print(f"  {row[f'{args.by}_count']:>8,}  {row['canonical_name']}{flag}")


def _cmd_templates_summary(args: argparse.Namespace) -> None:
    templates_dir: Path = args.templates_dir
    out_path: Path = args.out if args.out is not None else templates_dir / "TEMPLATES.md"
    lines = [
        "<!-- Generated by `uspto-assign templates-summary` — do not edit by hand. -->",
        "",
        "# Template steps summary",
        "",
        "One numbered line per step (what the UI steps list shows), plus the pre-run validation",
        "warnings for each template. Regenerate after any template change.",
        "",
    ]
    for path in sorted(templates_dir.glob("*.json")):
        try:
            templates = load_templates(path)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            print(f"skipping {path.name}: {type(exc).__name__}: {exc}")
            continue
        if not templates:
            continue
        lines.append(f"## {path.name}")
        lines.append("")
        for template in templates:
            lines.append(f"### {template.name}")
            lines.append("")
            for index, step in enumerate(template.steps, start=1):
                suffix = "" if step.enabled else "   *(disabled)*"
                lines.append(f"{index}. {describe_step(step)}{suffix}")
            lines.append("")
            warnings = validate_template(template.load, template.steps)
            if warnings:
                lines.append("**Warnings:**")
                lines.extend(f"- ⚠ {w}" for w in warnings)
            else:
                lines.append("**Warnings:** none")
            lines.append("")
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"templates summary -> {out_path}")


_DISPATCH = {
    "parse": _cmd_parse,
    "ingest": _cmd_ingest,
    "build-dictionary": _cmd_build_dictionary,
    "resolve": _cmd_resolve,
    "ledger": _cmd_ledger,
    "report": _cmd_report,
    "templates-summary": _cmd_templates_summary,
}


def main(argv: list[str] | None = None) -> None:
    """Dispatch a subcommand; a bare input path still works as the legacy ``parse``."""
    import sys  # noqa: PLC0415 - only needed when argv is None

    raw_args = list(sys.argv[1:] if argv is None else argv)
    # Backward compatibility: `uspto-assign file.xml --outdir out` == `uspto-assign parse …`.
    if raw_args and raw_args[0] not in _SUBCOMMANDS and not raw_args[0].startswith("-"):
        raw_args.insert(0, "parse")
    args = _build_parser().parse_args(raw_args)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    _DISPATCH[args.command](args)


if __name__ == "__main__":  # pragma: no cover - thin module runner
    main()
