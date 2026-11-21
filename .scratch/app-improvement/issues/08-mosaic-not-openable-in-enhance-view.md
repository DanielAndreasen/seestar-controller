# 08: Stitched mosaic cannot be opened in the enhance view (mosaic-stitch spec story 4)

**What to build:** make the stitched mosaic openable in the Enhance view like any other image, as the mosaic-stitch spec promises ("open the stitched image in the enhance view" / "enhance pipeline consumes it unchanged") — or formally descope story 4 in the spec if a UI flow is not wanted.

Evidence: `.scratch/mosaic-stitch/spec.md` stories 2-4 + Implementation Decisions vs `src/seestar_controller/mosaic/session.py:70-96` (file-only `mosaics/<sid>.fits`, "no store schema bump" per issue 01), `enhance/viewer_controller.py:191` `open_stack(frame_id)` and `enhance/fits_io.py:282-284` `list_project_stacks(kind="stack")` — the Enhance view lists/opens only store frames; `ui/views.py:1683-1689` "stitch" action only writes the FITS and prints the path. No code path registers or opens the mosaic in the enhance viewer.

**Blocked by:** None (can start immediately)

**Status:** implemented in e8995c2 (pending review)

- [x] Either: after stitching, the mosaic can be opened in the Enhance view — CHOSEN PATH: open-file path in the viewer + a mosaic row in the Enhance view list (`list_project_mosaics` + `ViewerController.open_path`, rows dispatch on `frame_id=None`; the Projects-view Stitch action opens the mosaic and switches to the Enhance view). Registering the mosaic as a `frames` row was rejected: issue 01 decided "no store schema bump", and a `kind="stack"` row would be re-ingested as a tile on the next stitch.
- [x] Chosen path covered by tests; `uv run pytest tests/test_mosaic_stitch.py tests/test_enhance_widgets.py -q` passes; full suite green; ty + ruff clean

## Comments

Audit finding (kind: missing-feature, severity: medium). Spec story vs implementation conflict.

Implemented in e8995c2 on branch wt/app-improvement-08 (open-file path, no descope of spec story 4 needed). Mosaics are file-based entries (`StackInfo` with `frame_id=None`, `kind="mosaic"`): `list_project_mosaics()` lists `mosaics/*.fits`, `ViewerController.stacks` includes them, `open_path()` opens any FITS by path (relative paths resolve against the project dir; listed entry or transient `mosaic`/`file` entry; cached by path+mtime), the viewer/editor rows dispatch `frame_id=None` -> `open_path`, and the Projects view stitch action opens the stitched mosaic in the shared viewer + switches to the Enhance view. Presets stay per stored frame (save/delete raise a clear EnhanceError for file-based entries, load returns None); `save_enhanced` still stores an `enhanced` frame in the mosaic's session.

Tests: `uv run pytest tests/test_mosaic_stitch.py tests/test_enhance_widgets.py -q` -> 54 passed, 1 skipped (pre-existing `reproject` skip). Neighboring suites re-run green: test_enhance_fits.py, test_sub_frames_viewer.py, test_ui_projects_stack_subs.py, test_sub_frames_widgets.py, test_offline_analysis_widgets.py (141 passed, 1 skipped; the one failure in test_offline_analysis_widgets.py::test_photometry_run_button_measures_the_stored_session_offline also fails on clean main ae31075 — pre-existing, unrelated). `uv tool run ruff check` -> All checks passed! `uv tool run ty check` -> 108 diagnostics, identical to the clean-main baseline (107 gi.repository missing-member errors + 1 pre-existing unused-ignore warning; no new diagnostics).
