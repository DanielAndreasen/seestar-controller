# 03: Mover overlay shows a blank frame counter — re-entrant `_render` clobbers the fresh state

**What to build:** make `MoverOverlayView` render "frame 1/6" (and keep the picture/status) on first auto-selection of a session, instead of leaving the frame label empty.

Evidence: `src/seestar_controller/moving_objects/overlay_view.py:361-395` — `_render` calls `set_selected(...)` which fires `notify::selected` → `_on_session_selected` → `controller.select_session()` → listener → nested `_render` with the fresh snapshot (sets paintable + label), then the outer render continues with its stale snapshot and executes `set_label("")`. Failing test: `tests/test_moving_objects_widgets.py::test_mover_overlay_view_lists_session_and_shows_frame` (`assert 'frame 1/6' in ''`). Same latent pattern at `src/seestar_controller/frame_quality/quality_view.py:466`.

**Blocked by:** None (can start immediately)

**Status:** completed — reviewed (approved) and merged to main in 4c31c47

- [x] After listing a session with frames, the frame label shows "frame 1/N" without any extra user action
- [x] `uv run pytest tests/test_moving_objects_widgets.py -q` passes (together with issue 09's test fixes); full suite green; ty + ruff clean

## Comments

Audit finding (kind: bad-implementation, severity: medium; real UI inconsistency, not just a test issue). Fix direction: block the `notify::selected` handler while updating the dropdown (`handler_block`), or re-render from the controller's current snapshot at the end of `_render`, or defer selection via `GLib.idle_add`.

Implemented in 226d69c (`src/seestar_controller/moving_objects/overlay_view.py` only; no test-file change needed — the frame-label assertion passes as-is). Chose the first fix direction: the `notify::selected` handler id is now stored and blocked around the WHOLE programmatic dropdown update (`set_model` already auto-selects and fires the notify — blocking only `set_selected` was not enough), the controller is driven explicitly via `select_session(...)`, and `_render` returns early when that changed the session, so the stale outer snapshot can never clobber the fresh nested/queued render. `refresh_sessions()` uses the same helper; user-driven dropdown changes still go through `_on_session_selected` unchanged. Verified in both notification modes (direct invoke = widget tests, `GLib.idle_add` = app) that the label shows "frame 1/6" and the paintable is set right after construction.

Test/lint results on branch wt/app-improvement-03 @ 226d69c: `uv run pytest tests/test_moving_objects_widgets.py tests/test_moving_objects.py -q` -> 66 passed, 5 failed — all 5 failures are exclusively issue 09's GTK3-only `.clicked()`/`.destroy()` lines (150, 163, 172, 202/209, 227, 243); every assertion up to and including `assert "frame 1/6" in view.frame_label.get_label()` now passes (previously `assert 'frame 1/6' in ''`). Re-running the 5 tests with 09-style GTK4 calls (`.emit("clicked")`, `view._on_destroy()`, scratch script only — the repo test file is untouched) passes the target test fully. `uv tool run ruff check` -> All checks passed. `uv tool run ty check` -> 108 diagnostics, identical to base (all pre-existing `gi.repository` missing-member noise from the gi-less sandbox; none on the changed lines). Full suite not run here (coordinator runs it at merge time).
