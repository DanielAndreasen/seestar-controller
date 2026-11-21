# 03: Mover overlay shows a blank frame counter — re-entrant `_render` clobbers the fresh state

**What to build:** make `MoverOverlayView` render "frame 1/6" (and keep the picture/status) on first auto-selection of a session, instead of leaving the frame label empty.

Evidence: `src/seestar_controller/moving_objects/overlay_view.py:361-395` — `_render` calls `set_selected(...)` which fires `notify::selected` → `_on_session_selected` → `controller.select_session()` → listener → nested `_render` with the fresh snapshot (sets paintable + label), then the outer render continues with its stale snapshot and executes `set_label("")`. Failing test: `tests/test_moving_objects_widgets.py::test_mover_overlay_view_lists_session_and_shows_frame` (`assert 'frame 1/6' in ''`). Same latent pattern at `src/seestar_controller/frame_quality/quality_view.py:466`.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] After listing a session with frames, the frame label shows "frame 1/N" without any extra user action
- [ ] `uv run pytest tests/test_moving_objects_widgets.py -q` passes (together with issue 09's test fixes); full suite green; ty + ruff clean

## Comments

Audit finding (kind: bad-implementation, severity: medium; real UI inconsistency, not just a test issue). Fix direction: block the `notify::selected` handler while updating the dropdown (`handler_block`), or re-render from the controller's current snapshot at the end of `_render`, or defer selection via `GLib.idle_add`.
