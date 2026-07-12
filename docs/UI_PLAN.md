# Plan — Metro-style PyQt6 desktop UI for the USPTO assignment toolkit

## Context & goal
Today the toolkit is a CLI (`main.py`) that streams USPTO assignment XML/ZIP into Parquet + Excel.
We want a **native desktop app** to *visually* work with the parsed data: browse the tables, apply
per-field filters, select rows, and import/export in multiple formats (including partial/filtered/
selected exports) — plus a panel that exposes the CLI's own options (formats, batch size, which
tables, output dir) so anything the "main function" does is drivable from the UI.

Two cross-cutting requirements shape every decision:
1. **Reusable-component architecture** — the parsing/table/filter/export logic must be UI-agnostic,
   plain, tested Python (a *service* layer), with the Qt layer only displaying state and forwarding
   events. This is the `ui-development` skill's core rule and is what lets us…
2. **…convert it to a Claude skill** later (in the patents Claude project): a `SKILL.md` + the
   installable reusable package + reference docs. Nothing UI-specific may leak into the core.

Non-goal: rewriting the parser. We **reuse** the existing extractors and dataclasses verbatim; we
only add in-memory table + filter + export services around them.

---

## 1. Package restructure (graduate from single-file to a package)
The README already says "if this outgrows a single file, graduate it." A UI + skill justifies it.
Move the proven logic out of `main.py` into an importable package; keep `main.py` as a thin shim so
the CLI and tests keep working.

```
assignment_uspto/
├── src/
│   ├── uspto_assignments/            # ── CORE (no Qt import anywhere) ──
│   │   ├── __init__.py               # re-export the public API
│   │   ├── model.py                  # dataclasses + TABLE_TYPES + FLAT_COLUMNS + arrow schemas
│   │   ├── parser.py                 # _stream_elements, iter_assignments, iter_records, extract
│   │   ├── tables.py                 # NEW: build in-memory pa.Table per table; TableBundle
│   │   ├── filters.py               # NEW: typed filter model → pyarrow.compute mask (pure, tested)
│   │   ├── exporters.py              # NEW: table-based writers (parquet/csv/xlsx/json/feather)
│   │   └── cli.py                    # argparse CLI (was main.py's main()/_build_parser())
│   └── uspto_assignments_ui/         # ── QT LAYER (imports core, never the reverse) ──
│       ├── __init__.py
│       ├── app.py                    # QApplication bootstrap; loads resources/metro.qss
│       ├── state.py                  # AppState dataclass = single source of truth
│       ├── controller.py             # event → update state → refresh (unidirectional)
│       ├── workers.py                # QThread parse/export workers (progress + finished signals)
│       ├── models/
│       │   └── arrow_table_model.py  # QAbstractTableModel over Arrow + filtered index (the crux)
│       ├── widgets/                  # reusable Metro components (see §5)
│       │   ├── page.py  section_label.py  filter_bar.py  data_table.py
│       │   ├── tile_grid.py  parse_panel.py  export_dialog.py  status_bar.py
│       └── resources/
│           └── metro.qss             # the single stylesheet (see §6)
├── main.py                           # shim: `from uspto_assignments.cli import main`
├── run_ui.py                         # entry point: launches the desktop app
├── tests/                            # core tests headless; UI tests via pytest-qt (optional)
└── pyproject.toml                    # add [project.scripts]; PyQt6 as an optional extra
```

Refactor mechanics: the current `_rows_to_table` and `_flat_rows` become **public** (`rows_to_table`,
`flat_rows`) in `tables.py`/`model.py` since the UI reuses them. `write_parquet`/`write_excel` stay
(stream-from-records path for huge files); `exporters.py` adds **table-based** writers for the
in-memory/interactive path. Keep `pyproject` `pythonpath`/packaging pointed at `src/`.

---

## 2. Core additions (all pure, all UptoQt-free, all unit-tested)

### `tables.py` — memory-mapped working store (DECISION: mmap, not full in-RAM)
The interactive store is **memory-mapped Arrow IPC (Feather v2)** — the one on-disk format pyarrow
can truly mmap for **zero-copy, low-RAM random access** (Parquet is compressed/encoded, so
`read_table` always materializes it into RAM; it stays an *export* format, not the working store).

- `parse_to_store(source: Path, store_dir: Path, *, progress=None) -> TableStore` — streams
  `iter_records()`, writes each table to `store_dir/<name>.arrow` via `pyarrow.ipc` in **batches**
  (bounded memory during parse, exactly like the current Parquet writer), calling `progress(n)`.
  **No Qt** — a plain callback.
- `open_store(store_dir) -> TableStore` — `pyarrow.feather.read_table(path, memory_map=True)` per
  table → OS pages data in on demand; RAM stays flat even for the 4 M-row `properties` table.
- `TableStore` = `{assignments, assignors, assignees, properties, flat}` of memory-mapped
  `pa.Table`s (identical schema/leading-zeros to the CLI output; reuse `rows_to_table` + schemas).
- The UI's "Open XML/ZIP" path calls `parse_to_store` into a chosen (or temp) dir, then `open_store`;
  "Open existing dataset" calls `open_store` directly (or converts a chosen Parquet/CSV dir once).

### `filters.py` — typed, vectorized filtering (the performance key)
QSortFilterProxyModel calls a Python predicate **per row** — unusable at 4 M rows. Instead filter in
`pyarrow.compute` (vectorized C++), returning the **row indices** that pass:
- `@dataclass FilterClause(column, op, value)` with `op ∈ {contains, equals, starts_with, not_empty,
  is_empty, in_range}` (`Literal`, not stringly-typed).
- `apply(table, clauses, *, quick_search=None) -> pa.Int64Array` — builds a boolean mask by AND-ing
  each clause (`pc.match_substring`, `pc.equal`, `pc.starts_with`, date range via lexicographic
  compare on the `YYYYMMDD` strings) plus an optional global substring search across string columns,
  then `pc.indices_nonzero(mask)`. Returns the **index array** the model will page through.
- `sort_indices(table, column, ascending) -> pa.Int64Array` via `pc.sort_indices`.
Both are trivially unit-tested with a tiny in-code table — no Qt, no files.

### `exporters.py` — multi-format, partial/selected
`export(table, path, fmt, *, rows: Sequence[int] | None = None)` where `rows` (when given) is the
filtered-or-selected index subset (`table.take(rows)` first). `fmt ∈ {parquet, csv, xlsx, json,
feather}`:
- parquet → `pq.write_table`; feather → `feather.write_feather`; csv → `pyarrow.csv.write_csv`;
  json → `pa.Table` → row dicts → `json`; xlsx → openpyxl `write_only` (reuse the streaming approach
  already proven, sourced from the Arrow rows). Excel row-cap guard reused.
- This is what powers "export partial or selected files": the UI passes the current view's index
  array (filters applied) or the selection's row ids.

---

## 3. Qt architecture — state, threading, model/view

### State & flow (from the `ui-development` skill)
`AppState` (plain dataclass in `state.py`) is the **single source of truth**: loaded `TableBundle`,
`current_table`, `list[FilterClause]`, `quick_search`, `sort`, `selection`. `controller.py` is the
only thing that mutates it; every user action goes **event → controller updates state → view
re-reads state**. Widgets never mutate each other. This is also what makes the logic testable and
skill-portable.

### Never block the GUI thread
Parsing the 1.4 GB file takes minutes. `workers.py` runs it in a `QThread` worker object emitting
`progress(int)`, `finished(TableBundle)`, `failed(str)`; the window shows a Metro `QProgressBar` and
handles **all four async states** (idle / loading / error / success) — no spinner-forever, no blank
screen on a bad file. Export runs in the same worker pattern.

### The model — QAbstractTableModel over Arrow + a filtered index (handles millions)
`ArrowTableModel(QAbstractTableModel)` holds:
- `_table: pa.Table` (the current memory-mapped table from the `TableStore`),
- `_view: pa.Int64Array` — the ordered row indices after filter+sort (defaults to `0..n`).

`rowCount = len(_view)`, `columnCount = _table.num_columns`. `data(index, DisplayRole)` maps
`view_row → _table.column(col)[_view[row]].as_py()`. Because Qt only queries the ~50 visible cells,
this is O(visible), not O(rows) — millions of rows scroll smoothly with no proxy model.
`set_filter(indices)` / `set_sort(indices)` swap `_view` and emit `layoutChanged` (or `beginResetModel`).
All filtering/sorting happens in `filters.py` (vectorized), never per-row in Python.

Row height 38px and other **metrics** are set via `verticalHeader().setDefaultSectionSize(38)` and
`setFixedSize` for tiles — these are layout metrics, not colours/styling, so they stay out of QSS
(QSS keeps all colour/border/fill; see §6). Full-row selection + selection colour come from QSS.

---

## 4. Feature → implementation map (everything the request lists)

| Feature | How |
|---|---|
| Visually browse tables | `QTabWidget` (one tab per table) each hosting a `DataTable` (styled `QTableView` + `ArrowTableModel`) |
| Apply field filters | `FilterBar` builds `list[FilterClause]` → `filters.apply()` → `model.set_filter()`; live row-count label |
| Quick search | Debounced search box → `quick_search` in state → same `apply()` path |
| Sort | Click header → `filters.sort_indices` → `model.set_sort()` |
| Import XML/ZIP | `ParsePanel` → parse worker → `build_bundle` → populate tabs |
| Import existing data | Open an Arrow-IPC store (`open_store`, mmap) or convert a `.parquet`/`.csv`/`.feather` dir once → tabs |
| Export multiple formats | `ExportDialog`: pick table(s), format, scope |
| Export partial / selected | scope = **All / Filtered view / Selected rows**; passes the right index array to `exporters.export` |
| Configure the "main function" | `ParsePanel` surfaces CLI options: formats, `--batch-size`, output dir, which tables, verbose → same code path as `cli.main` |
| Progress & feedback | Metro `QProgressBar`, status bar messages, disabled controls while working |

---

## 5. Reusable Metro components (small, composable, QSS-driven)
Each is a thin `QWidget`/`QPushButton` subclass carrying **dynamic properties** so the QSS has zero
per-widget IDs (per the tile rule, applied everywhere):
- `MetroPage(title, body)` — page title (`property("role","h1")`, 26px) + content region.
- `SectionLabel(text)` — 11px 600 uppercase, letter-spacing 1px (**see §6 caveat**).
- `PrimaryButton` / `MetroButton` — `property("primary","true")` variant.
- `MetroInput` (`QLineEdit`), `MetroCombo` (`QComboBox`).
- `DataTable` — preconfigured `QTableView` (no grid, 38px rows, full-row select, header rule).
- `FilterBar` — column combo + operator combo + value input + Add/Clear; emits `filtersChanged`.
- `Tile` — 140×140 `QPushButton`, `property("tile","true")` + `property("variant", ...)` for the
  accent/neutral colour variants; used on a `TileGrid` landing page (Open XML / Open dataset /
  Export / Recent).
- `ParsePanel`, `ExportDialog`, `StatusBar`.
Components expose Python signals and read/write `AppState` only through the controller.

---

## 6. `metro.qss` design (single stylesheet, `app.setStyleSheet(metro.qss)`)
Loaded once in `app.py` from `resources/metro.qss` via `Path.read_text()`. **All** colour/border/
fill live here; **no** inline `setStyleSheet`, **no** hex in Python. Palette tokens used exactly as
specified. Key selectors (abridged):

```css
* { border-radius: 0px; }                                   /* no rounded corners anywhere      */
QWidget { background:#FFFFFF; color:#1D1D1D;
          font-family:"Segoe UI","Selawik","Noto Sans"; font-size:12px; }
QWidget#chrome, QMenuBar, QStatusBar { background:#F0F0F0; }

QLabel[role="h1"]   { font-size:26px; }                     /* weight set in Python — see caveat */
QLabel[role="section"] { font-size:11px; font-weight:600; color:#6D6D6D; } /* uppercase in Python */

QPushButton { background:#CCCCCC; color:#1D1D1D; border:none; padding:6px 16px; }
QPushButton:hover   { background:#BFBFBF; }
QPushButton:pressed { background:#0078D7; color:#FFFFFF; }  /* invert to accent */
QPushButton[primary="true"] { background:#0078D7; color:#FFFFFF; }

QLineEdit, QComboBox { background:#FFFFFF; border:2px solid #ABABAB; padding:5px; }
QLineEdit:hover, QComboBox:hover { border-color:#6D6D6D; }
QLineEdit:focus, QComboBox:focus { border-color:#0078D7; } /* focus = border colour, no glow */

QTabBar::tab { background:transparent; border:none; border-bottom:3px solid transparent;
               padding:8px 18px; color:#6D6D6D; }
QTabBar::tab:selected { border-bottom-color:#0078D7; color:#1D1D1D; }

QHeaderView::section { background:#F0F0F0; border:none; border-bottom:1px solid #ABABAB;
                       padding:8px; }
QTableView { background:#FFFFFF; gridline-color:transparent; }
QTableView::item:selected { background:#0078D7; color:#FFFFFF; }   /* full-row accent */

QScrollBar:vertical { width:14px; background:#F0F0F0; }
QScrollBar::handle:vertical { background:#CDCDCD; }                /* flat, square */
QScrollBar::add-line, QScrollBar::sub-line { height:0; width:0; } /* zero-size arrows */

QProgressBar { background:#E6E6E6; border:none; text-align:center; }
QProgressBar::chunk { background:#0078D7; }

QPushButton[tile="true"] { min-width:140px; max-width:140px; min-height:140px; max-height:140px;
                           background:#CCCCCC; border:none; text-align:left; padding:10px; }
QPushButton[tile="true"][variant="accent"]  { background:#0078D7; color:#FFFFFF; }
QPushButton[tile="true"][variant="neutral"] { background:#E6E6E6; }
```

**Two honest Qt-QSS limitations (documented, handled in components not by hex-in-Python):**
1. Qt QSS has **no `letter-spacing`** and **no `text-transform`**. So `SectionLabel` uppercases its
   text in Python and sets `QFont.setLetterSpacing(1px)`; the *colour/size/weight* stay in QSS.
2. Numeric `font-weight:300` is unreliable across Qt6 QSS builds; since the **light weight is the
   signature of the title**, `MetroPage` sets the title `QFont` weight to `Light` explicitly. Size
   and colour remain in QSS. (These are font *metrics*, not the forbidden hardcoded hex/inline-QSS.)

We tile-select colours via dynamic properties exactly as requested; `app.py` calls
`style().polish(widget)` after changing a property so restyling takes effect live.

---

## 7. Convert to a Claude skill (patents project)
Because the core is Qt-free and importable, packaging is mechanical:
```
patent-assignment-toolkit/            # skill dir dropped into the patents project's .claude/skills/
├── SKILL.md                          # frontmatter name/description + "use when parsing/exploring
│                                     #   USPTO assignment XML, building/launching the viewer, or
│                                     #   exporting filtered subsets"; routing + quickstart
├── references/
│   ├── architecture.md               # this plan, trimmed to the shipped design
│   ├── metro-qss-spec.md             # the palette + component rules (verbatim brief)
│   └── data-schema.md                # the 5 tables + column meanings
└── (installs) uspto_assignments[+ui] # the package via pyproject extras
```
The skill's "scripts" are just the package entry points (`uspto-assign` CLI, `uspto-assign-ui`).
Keeping `uspto_assignments` (core) and `uspto_assignments_ui` (Qt) as **separate importable
packages** means the skill can offer the parser/export capability even in headless contexts and the
UI only when a desktop is available.

---

## 8. Dependencies
- Core (unchanged + already present): `lxml`, `pyarrow`, `openpyxl`.
- UI extra: `PyQt6` (native widgets). Optional dev: `pytest-qt` for UI smoke tests.
- Declared as extras in `pyproject.toml`:
  `[project.optional-dependencies] ui = ["PyQt6>=6.6"]`, `dev = [... , "pytest-qt"]`.
  Core stays installable without Qt (headless/skill/CI).

## 9. Testing
- **Core (headless, the bulk):** `tables.build_bundle` row counts vs. the CLI; `filters.apply` for
  each operator + quick-search + date range; `filters.sort_indices`; `exporters.export` round-trips
  for every format incl. `rows=` subset (partial/selected). All pure — no Qt, fast.
- **Model:** drive `ArrowTableModel` with `QAbstractItemModelTester`; assert `data()` maps through
  the filtered index correctly after `set_filter`.
- **UI smoke (optional, pytest-qt):** app launches, loads the fixture, a filter shrinks the row
  count, an export writes a file. Kept minimal — logic is already covered headless.
- Existing 14 parser tests move with the package and keep passing.

## 10. Verification (end-to-end, per DoD)
1. `ruff format` + `ruff check` clean; `pyright` strict clean (Qt: add `PyQt6` stubs are bundled).
2. `pytest` green (core + model + smoke).
3. `python run_ui.py` → open `tests/fixtures/sample_assignment.xml`, confirm: tabs populate, a
   `FilterBar` clause reduces the visible count, sort works, export **filtered** and **selected**
   subsets to parquet+csv+xlsx and reload them to verify contents.
4. Metro visual pass against the brief: screenshot; check 0px radius, flat fills, accent only on the
   six allowed surfaces, 38px rows, 140px tiles, focus-by-border.
5. Large-file sanity: open the real ZIP; confirm progress bar advances, GUI stays responsive
   (parse on worker thread), memory bounded (~hundreds of MB for the in-memory bundle).

---

## 11. Phased delivery (each phase independently runnable)
1. **Refactor to package** (no behaviour change): move core out of `main.py`, keep CLI + 14 tests
   green. *Ship-safe checkpoint.*
2. **Core services**: `tables.py`, `filters.py`, `exporters.py` + their headless tests.
3. **Model + skeleton app**: `ArrowTableModel`, `app.py`, `metro.qss`, a window that loads the
   fixture into tabbed tables. Metro look locked in.
4. **Interactivity**: `FilterBar`, quick search, sort, selection, status/progress, threaded parse.
5. **Import/Export**: `ExportDialog` (formats × scope), open-existing-dataset, `ParsePanel` config.
6. **Tiles landing page + polish**; optional pytest-qt smoke; screenshots.
7. **Skill packaging**: `SKILL.md` + references + extras wiring.

## 12. Locked decisions (confirmed)
- **Restructure to the `src/` package** (core `uspto_assignments` + `uspto_assignments_ui`); `main.py`
  becomes a CLI shim.
- **Memory-mapped working store**: parse → Arrow IPC (Feather) files → `read_table(memory_map=True)`;
  low, flat RAM. Parquet stays an *export/output* format, not the working store.
- **Export formats**: Parquet, Excel (.xlsx), CSV, JSON, Feather — each honoring All / Filtered view /
  Selected rows scope.
```
