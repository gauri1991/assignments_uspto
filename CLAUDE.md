# assignment_uspto

Type: script. Purpose: parse USPTO patent-assignment XML into normalized Parquet + Excel tables
(streaming parse for large bulk files). Entry point: `main.py`.

Inherits the pycoder workspace standards, skills, and settings from the parent directory
(../CLAUDE.md and ../.claude/). Follow those. Project-specific notes below.

## Project notes

- **Tooling: venv + pip** (not uv — uv was not installed when this was scaffolded). Activate/install:
  `python3 -m venv .venv && .venv/bin/pip install lxml pyarrow openpyxl pytest ruff pyright
  lxml-stubs pyarrow-stubs`. If you install uv later, migrate with `uv init` + `uv add`.
- Runtime deps: `lxml` (streaming parse), `pyarrow` (Parquet), `openpyxl` (streaming Excel).
  The `*-stubs` packages are dev-only, needed for `pyright` strict to pass on those libraries.
- Install (editable, with dev extras): `.venv/bin/pip install -e ".[dev]"`
- Run CLI: `.venv/bin/python main.py path/to/assignment.xml --outdir out` (or `.venv/bin/uspto-assign ...`)
- Test: `.venv/bin/pytest`
- Lint/format: `.venv/bin/ruff format . && .venv/bin/ruff check --fix .`
- Types: `.venv/bin/pyright`
- Layout: `src/` package `uspto_assignments` (core, Qt-free): `model` (row types/schemas),
  `parser` (streaming XML/ZIP parse), `tables` (record→table + memory-mapped Arrow-IPC store),
  `filters` (vectorized pyarrow.compute filter/sort), `exporters` (parquet/csv/xlsx/json/feather),
  `cli`. `main.py` is a thin CLI shim. The desktop UI (`src/uspto_assignments_ui`, PyQt6) is being
  built per `docs/UI_PLAN.md` and imports the core, never the reverse.
- pyarrow note: `pyarrow.compute` is under-typed in pyarrow-stubs; `filters.py` routes it through
  an `Any` view (documented in-file) so pyright stays strict everywhere else.
