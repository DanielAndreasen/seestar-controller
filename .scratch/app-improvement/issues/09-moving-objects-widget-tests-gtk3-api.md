# 09: moving-objects widget tests use GTK3-only APIs (`.clicked()`, `.destroy()`) and can never pass on GTK4

**What to build:** fix `tests/test_moving_objects_widgets.py` to use the repo's documented GTK4 conventions so the mover overlay's detect/lookup/blink/refresh behavior is actually verified wherever gi is importable.

Evidence: `tests/test_moving_objects_widgets.py:150,163,172,202,209,227,243` call `view.detect_button.clicked()`, `view.lookup_button.clicked()`, `view.destroy()` → `AttributeError: 'Button' object has no attribute 'clicked'` / `'MoverOverlayView' object has no attribute 'destroy'` (GTK 4.14.5). Sibling suites document the correct convention: use `.emit("clicked")` and the view's own destroy handler (`tests/test_gaia_overlay_widgets.py:149,216`, `tests/test_astrometry_view_widgets.py:31-32,279`, docstring in `tests/test_sub_frames_analysis_widgets.py:11`). Failing tests: `test_mover_overlay_view_detect_button_finds_mover`, `::test_mover_overlay_view_lookup_button_records_match`, `::test_mover_overlay_view_blink_steps_frames`, `::test_mover_overlay_view_refresh_and_selection` (plus `::test_mover_overlay_view_lists_session_and_shows_frame`, covered by issue 03). Contradicts `.scratch/moving-objects/issues/02-mpc-overlay.md`'s "full suite green" claim — it only holds where the module skips.

**Blocked by:** None (can start immediately); issue 03 fixes the remaining src-side failure in the same file

**Status:** ready-for-agent

- [ ] All tests in `tests/test_moving_objects_widgets.py` use `.emit("clicked")` and GTK4-correct teardown and pass with gi importable
- [ ] Full suite `uv run pytest -q -n auto` green; ty + ruff clean

## Comments

Audit finding (kind: failing-test / test bug, severity: medium). Test-side only — no src change in this ticket.
