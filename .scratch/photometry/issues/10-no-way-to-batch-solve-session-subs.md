# Issue 10: no way to batch plate-solve a session's subs — WCS reprojection (issue 09) only helps the few solved frames

**Status:** completed (fixed in 00ca04f on branch wt/photometry-10: pure `solve_session_unsolved()` + `session_wcs_coverage()` in astrometry/session.py with `call_store` marshalling of `solve_project_frame`, RunController wrappers with the run-or-solve busy guard, and a "Solve unsolved subs" button + "WCS: N of M subs solved" coverage label in the photometry Run tab; not merged to main yet)

## Found while
Live session 2026-11-22, project `vw-cep`, session `20261122T192149035964Z` (512 subs). After issue 09 (per-frame WCS reprojection of sky picks, merged in 14ce236) the photometry run can only reproject frames that HAVE a stored WCS — `frame_wcs` has 4 rows out of 512, so a re-run would still measure only a handful. The only solve UI is the Enhance view's Solve panel, one open frame at a time; there is no "solve all subs of this session".

## Expected
- Run photometry tab (and/or Enhance view): a "Solve unsolved subs" action for the selected session that plate-solves every sub without a `frame_wcs` row via `seestar_controller.astrometry.session.solve_project_frame` / `solve_session_frame`, on a worker thread with progress (done/total, current frame), never blocking the GTK main loop, per-frame failures recorded and skipped (never kill the batch), db writes marshalled to the db-owning thread like `RunController._call_store`.
- Optionally a stride/sample control (e.g. every Nth sub) since drift is smooth — but full solve must be the default.
- After solving, the Run photometry tab shows WCS coverage ("508 of 512 frames solved") and re-Run measures via reprojection.

## Scope
`src/seestar_controller/photometry/run.py` (or a new pure batch-solve helper in `astrometry/session.py`) + `results_view.py` / pick-run UI wiring, tests in `tests/test_photometry_*.py` / `tests/test_astrometry_session*.py` (mock solver, per-frame failure tolerance, progress sequence, db-thread marshalling).
