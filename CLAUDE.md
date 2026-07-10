# assignment_uspto

Type: script. Purpose: USPTO patent assignment script _(update this line with the real purpose)_.

Inherits the pycoder workspace standards, skills, and settings from the parent directory
(../CLAUDE.md and ../.claude/). Follow those. Project-specific notes below.

## Project notes

- **Tooling: venv + pip** (not uv — uv was not installed when this was scaffolded). Activate/install:
  `python3 -m venv .venv && .venv/bin/pip install pytest ruff pyright`. If you install uv later,
  you can migrate with `uv init` + `uv add`.
- Run: `.venv/bin/python main.py`
- Test: `.venv/bin/pytest`
- Lint/format: `.venv/bin/ruff format . && .venv/bin/ruff check --fix .`
- Types: `.venv/bin/pyright`
- Layout: flat single-file (`main.py`), no `src/` package — this is a simple script.
