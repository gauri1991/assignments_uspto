---
name: verify
description: How to verify UI/core changes in this repo by driving the real PyQt6 app headlessly (offscreen screenshots + scripted interaction), not by re-running tests.
---

# Verifying changes in assignment_uspto

The UI surface is PyQt6; there is no display in most sessions. Drive it with
`QT_QPA_PLATFORM=offscreen` — widgets render fully and `widget.grab().save("x.png")`
produces real screenshots you can view.

## Recipe

1. Write a driver script in the scratchpad (not the repo). Skeleton:
   - `sys.path.insert(0, "<repo>/src")`, then `from uspto_assignments_ui.app import create_app`.
   - `app = create_app([])` applies the Metro stylesheet (same entry the real app uses).
   - Instantiate real widgets (`BatchDialog(BatchTemplateStore(tmp/"t.json"), cpc_store=CpcConfigStore(tmp/"c.json"))`),
     call `.show()`, `app.processEvents()`, then `.grab().save(...)` for screenshots.
   - Drive interactions through real widgets: `button.click()`, `combo.setCurrentText(...)`,
     `item.setCheckState(...)` — not by calling private slots when a widget path exists.
2. For background runs (BatchWorker/QThread): after `run_btn.click()`, spin the loop:
   `while cond: app.processEvents(); time.sleep(0.02)` until `dialog._thread is None`.
3. To auto-answer a modal `QMessageBox` (e.g. the close-during-run prompt):
   arm `QTimer.singleShot(100, answer)` **before** triggering it; in `answer`, find
   `app.activeModalWidget()`, click the standard button via
   `box.standardButton(child) == QMessageBox.StandardButton.Yes`; re-arm if not found yet.
4. Test inputs: `tests/fixtures/sample_assignment.xml` (tiny, 2 assignments). Copy it under
   different names/dirs for multi-input batch runs. Run everything in a `tempfile.mkdtemp` dir.
5. Run with the project venv: `QT_QPA_PLATFORM=offscreen .venv/bin/python driver.py`.

## Gotchas

- **BatchDialog persists the entity memory on every finished run** (`_on_finished` →
  `EntityMemoryStore().save(...)`). Passing no `memory_store` uses the USER'S REAL store
  (~90k aliases). Always pass `EntityMemoryStore(tmp_path / "entities.json")` in drivers.
- The tiny fixture finishes in ~0.1s/file — cancellation/close-guard races resolve before
  you can observe intermediate states. Use 5+ input copies and hook
  `worker.batch_event` to time interactions, and accept that the run may complete first.
- "This plugin does not support propagateSizeHints()" on stderr is offscreen-platform noise.
- Console text is the best runtime evidence for batch flows: `dialog._console.toPlainText()`.
