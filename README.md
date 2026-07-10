# assignment_uspto

Type: **script**. Purpose: USPTO patent assignment script _(placeholder — update this line)_.

A minimal single-file Python script with linting, typing, and tests configured.

## Setup

This project uses **venv + pip** (uv was not installed at scaffold time).

```bash
python3 -m venv .venv
.venv/bin/pip install pytest ruff pyright
```

## Run

```bash
.venv/bin/python main.py 3 7 11        # summarize the given numbers
.venv/bin/python main.py               # runs on a default sample
.venv/bin/python main.py -v 3 7 11     # with debug logging
```

## Develop

```bash
.venv/bin/ruff format . && .venv/bin/ruff check --fix .   # format + lint
.venv/bin/pyright                                         # type-check
.venv/bin/pytest -q                                       # test
```

Put real logic in small typed functions in `main.py` (test them in `tests/`), keep `main()` thin.
If this outgrows a single file, graduate it to a `library` or `cli` project.
