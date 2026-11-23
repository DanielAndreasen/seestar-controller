# Issue 11: run-progress label has the same Frame-repr leak as the solve label fixed in issue 10 follow-up

**Status:** completed on branch `wt/photometry-11` (fix commit 4a374af, not yet merged to main): `_on_progress`/`_render_progress` now take `frame: Frame` and the label renders `frame.id` (same fix as d713174); `test_results_panel_run_progress_label_shows_the_frame_id` in tests/test_photometry_widgets.py asserts the mid-run label is `measuring 3/4 — frame <id>` with no `"Frame("`

## Found while
Review follow-up of issue 10 (wt/photometry-10, commit d713174 fixed the SOLVE progress label). The measure/run path has the same latent pattern: `run.py`'s `ProgressCallback` is `Callable[[int, int, Frame], None]` and is invoked with a `Frame`, but `results_view.py`'s `_on_progress`/`_render_progress` still annotate the third arg as `frame_id: int` and interpolate it directly — so the run progress label shows the full `Frame(...)` dataclass repr instead of the frame id.

## Expected
- `_on_progress`/`_render_progress` take `frame: Frame` and the label renders `frame.id` (same fix as d713174).
- A widget test asserting the mid-run label is `... frame <id>` and contains no `"Frame("`.

## Scope
`src/seestar_controller/photometry/results_view.py`, `tests/test_photometry_widgets.py`.
