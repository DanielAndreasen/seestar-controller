# 08: Show the first stored sub as preview in the Observe view

**Found during:** live guided test (project ngc869, 2026-11-22), indoors with the solar filter. The Observe view's right-pane preview is fed only by the scope's live preview stream (`link.preview.PreviewStream`); when that stream is absent (or not informative, e.g. solar filter indoors) the pane stays on "no preview yet" even though subs are being stored. The user wants to see the **first stored sub/exposure** in the Observe page to confirm there is light on the sensor.

**What to build:** when the first sub of the current session is stored (`SessionCapture` -> `store_frame`), load it (downscaled) and display it in the Observe view preview area (fallback when the live preview stream has no frame yet, or a dedicated "first exposure" thumbnail next to the live preview). Must be main-loop safe (`invoke_on_main`) and cheap (decode once, cache texture). Acceptance: start a night, wait for sub 1 to be stored -> Observe view shows that image even with the scope preview stream idle; live preview still takes over once streaming.

**Blocked by:** None (observing 01-05 done; relates to 07 live refresh)

**Status:** implemented in 2aa35b3 (pending review)

- [x] Observe view can render a stored FITS (downscaled) as preview fallback / first-exposure thumbnail
- [x] Wired to the first stored frame of the running session
- [x] Tests: store first sub -> observe view preview updates without live stream
- [x] Full suite green

## Comments

Implemented on branch `wt/fix-obs-08` (worktree `fix-obs-08`), commit `2aa35b3`:

- `observing/observe_controller.py`: new pure loader `load_first_sub_frame(project, session_id)` takes the first sub (`list_frames(..., kind="sub")[0]`, jd order), reads the FITS once (lazy astropy), percentile-stretches to uint8 (`FIRST_SUB_STRETCH_PERCENTILES = (1.0, 99.0)`) and downscales with PIL to <= `FIRST_SUB_PREVIEW_MAX_PX = 512`; returned as a frozen `FirstSubFrame` mirroring the `PreviewFrame` shape the view renders. The controller listens to the `SessionCapture` log (`KIND_FRAME` events) and marshals the load via `invoke_on_main` (`_on_capture_log_event` -> `_load_first_sub_frame`), caching the result so it is decoded once per night; `first_sub_frame` is reset in `start_night` and carried in `ObserveViewState.first_sub_frame`.
- `observing/observe_view.py`: the preview picture now renders `state.preview_frame` when present, else `state.first_sub_frame` (same render path, re-render keyed on `(source, id)`), so the stored first sub shows as the fallback while the live stream is idle and the live preview takes over once it streams; the "no preview yet" placeholder only shows when neither is set.
- Tests (`tests/test_observing_first_sub.py`, new): loader unit tests (first-by-jd, downscale, uint8, no-sub -> None, RGB FITS), controller wiring with a fake device/capture and a never-emitting preview stream (first sub stored -> `snapshot.first_sub_frame` set while `preview_frame` is None; second sub does not replace it; the load is queued via `invoke_on_main`, not inline; live `stream.emit` takes over; reset on next night), plus a widget test driving `ObserveView` fallback -> live -> fallback -> placeholder.

Verification (worktree `~/.vibe/worktrees/seestar-controller-dec7d727fea1/fix-obs-08`, venv with `--system-site-packages` + pygobject/pycairo so gi is present): all observing test files pass (121 passed incl. the 7 new), `uv tool run ruff check` clean, `uv tool run ty check` -> 108 diagnostics, identical to pristine main (pre-existing gi-stub noise). Full `uv run pytest -q -n auto` runs green apart from pre-existing flaky-under-xdist tests in unrelated modules (visibility/transport/preview threads; they pass when run individually and the failing set varies between runs, also on the baseline).
