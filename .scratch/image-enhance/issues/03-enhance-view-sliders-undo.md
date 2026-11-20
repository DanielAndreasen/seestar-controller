# 03: Enhance view — sliders, before/after, undo/redo

**What to build:** the Enhance view UI — per-op sliders, live before/after comparison, undo/redo of parameter changes. Demoable: open a stack, move sliders, image updates live, undo restores previous state.

**Blocked by:** 01, 02, ui-shell 01

**Status:** implemented in 3519c44bcf6719ddf3918d6feaa165d9592084c9

- [x] Slider per pipeline parameter, live re-render
- [x] Before/after toggle or split view
- [x] Undo/redo stack for parameter changes
- [x] UI stays responsive during re-render (worker thread)
- [x] Tests: view controller with mock pipeline; undo/redo logic

## Comments

- Pure layer `enhance/editor_controller.py` (no gi): `EditorController(viewer_controller, *, invoke_on_main=None, render_max_size=1024, debounce_s=0.05, render_fn=None)` follows the shared `ViewerController`'s open stack; `set_param(op, **changes)` updates the pipeline via `Pipeline.with_param`, pushes the previous pipeline onto a bounded undo stack (`MAX_UNDO_ENTRIES=50`, oldest evicted), clears redo, and schedules a debounced re-render; `undo()`/`redo()`/`reset()` swap stacks and re-render; listeners get a frozen `EditorSnapshot(stack, pipeline, enhanced_preview, original_preview, rendering, can_undo, can_redo, error)` — before/after is just `original_preview` (raw FITS via `render_preview`) vs `enhanced_preview` (via `render_pipeline`).
- Re-render never blocks: worker `threading.Thread` (same pattern as `ui/connection.py`), result marshalled via `invoke_on_main` (app binds `GLib.idle_add`); `threading.Timer` debounce coalesces rapid slider moves into one render (latest wins) and a generation counter discards stale results; render errors land in `snapshot.error` + notify, never crash.
- Widget layer `enhance/enhance_view.py` (gi at top, lazy export): `EnhanceEditor` — left pane = stack list + Refresh + one `Adw.PreferencesGroup` per pipeline op with a `Gtk.Scale` per numeric param (ranges in `PARAM_RANGES`) and a `Gtk.DropDown` per choice param (`PARAM_CHOICES`), plus 3 scales for `white_balance.gains`; right pane = Before/After toggle switching a `Gtk.Stack` of the two previews + Undo/Redo/Reset buttons (sensitivity from `can_undo`/`can_redo`) + `Gtk.Spinner` while `rendering`. Wired into `ui/views.py` `EnhanceView`/`build_view` and `ui/app.py` (app owns the shared `EditorController` over the shared `ViewerController`).
- Tests: `tests/test_enhance_editor.py` (23 pure headless tests: undo/redo exactness + bound, debounce coalescing, generation/stale discard, queued invoke_on_main, non-blocking set_param, error snapshot, listener isolation, frozen snapshot, no-gi subprocess + source scan, real-ViewerController integration); boundary lists in `tests/test_enhance_fits.py` extended (`editor_controller` pure, `enhance_view` widget); `tests/test_enhance_widgets.py` wiring tests updated + new slider/toggle/button widget tests (skip headless without gi). `uv run pytest`: 1287 passed, 3 skipped (the 3 gi widget modules).
- Assumptions: debounce 0.05 s, undo bound 50, reset is undoable, equal-value set_param is a no-op, invalid changes raise `EnhanceError` and leave undo untouched, widget tests skip in the headless sandbox.
