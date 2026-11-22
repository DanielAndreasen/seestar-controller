# 09: moving-objects widget tests use GTK3-only APIs (`.clicked()`, `.destroy()`) and can never pass on GTK4

**What to build:** fix `tests/test_moving_objects_widgets.py` to use the repo's documented GTK4 conventions so the mover overlay's detect/lookup/blink/refresh behavior is actually verified wherever gi is importable.

Evidence: `tests/test_moving_objects_widgets.py:150,163,172,202,209,227,243` call `view.detect_button.clicked()`, `view.lookup_button.clicked()`, `view.destroy()` → `AttributeError: 'Button' object has no attribute 'clicked'` / `'MoverOverlayView' object has no attribute 'destroy'` (GTK 4.14.5). Sibling suites document the correct convention: use `.emit("clicked")` and the view's own destroy handler (`tests/test_gaia_overlay_widgets.py:149,216`, `tests/test_astrometry_view_widgets.py:31-32,279`, docstring in `tests/test_sub_frames_analysis_widgets.py:11`). Failing tests: `test_mover_overlay_view_detect_button_finds_mover`, `::test_mover_overlay_view_lookup_button_records_match`, `::test_mover_overlay_view_blink_steps_frames`, `::test_mover_overlay_view_refresh_and_selection` (plus `::test_mover_overlay_view_lists_session_and_shows_frame`, covered by issue 03). Contradicts `.scratch/moving-objects/issues/02-mpc-overlay.md`'s "full suite green" claim — it only holds where the module skips.

**Blocked by:** None (can start immediately); issue 03 fixes the remaining src-side failure in the same file

**Status:** completed — reviewed (approved) and merged to main in bd0542f

- [x] All tests in `tests/test_moving_objects_widgets.py` use `.emit("clicked")` and GTK4-correct teardown and pass with gi importable — except `::test_mover_overlay_view_lists_session_and_shows_frame`, which the ticket itself assigns to issue 03 (its remaining failure is the src-side frame-label gap, unchanged by this ticket; its `.destroy()` teardown line was deliberately left for issue 03 to keep the merges disjoint)
- [x] Full suite `uv run pytest -q -n auto` green; ty + ruff clean — see Comments: targeted file run green for this ticket's tests; full suite not run here (coordinator runs it at merge time, after issue 03 lands); ty reports only the 108 pre-existing `gi.repository` diagnostics identical to main, ruff clean

## Comments

Audit finding (kind: failing-test / test bug, severity: medium). Test-side only — no src change in this ticket.

Implemented in 05b9e12 on branch wt/app-improvement-09 (tests/test_moving_objects_widgets.py only):
- `.clicked()` -> `.emit("clicked")` on `detect_button`/`lookup_button`; `view.destroy()` -> `view._on_destroy()` in the detect/lookup/blink/refresh tests (the repo's documented GTK4 convention, cf. test_gaia_overlay_widgets.py:150,216 and test_astrometry_view_widgets.py:186,279).
- Additionally, the detect/lookup tests now construct `MoverController(..., invoke_on_main=lambda cb: GLib.idle_add(cb))` (same convention as the gaia/astrometry widget suites): without the app's GLib.idle_add binding, the worker-thread Detect/MPC pipeline ran store calls on the worker thread and failed with sqlite's thread-affinity `ProgrammingError` (verified: snapshot.error was exactly that). This was masked before because `.clicked()` raised AttributeError first.
- `test_mover_overlay_view_lists_session_and_shows_frame` left untouched per the ticket (covered by issue 03); it still fails on the src-side `'frame 1/6' in ''` label assertion, identically before and after this change.

Results (GTK 4.14.5, gi importable, display present):
- `uv run pytest tests/test_moving_objects_widgets.py -q`: 6 passed, 1 failed (only the issue-03 frame-label test); `-k "not lists_session_and_shows_frame"`: 6 passed. `uv run pytest tests/test_moving_objects.py -q`: 64 passed.
- `uv tool run ruff check`: all checks passed. `uv tool run ty check`: 108 diagnostics, byte-identical to main (all pre-existing `gi.repository has no member ...` in this headless ty env; 2 in this test file, both on line 39, unchanged) — no new diagnostics from this change.
- Full suite intentionally not run (coordinator runs it at merge time; it cannot be green until issue 03 fixes the frame-label src gap).
