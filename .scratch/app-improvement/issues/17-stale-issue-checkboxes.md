# 17: "implemented (merged)" issue files still show every acceptance checkbox unchecked

**What to build:** tick the acceptance checkboxes (and fill the "(implementor fills in)" comments) on issue files whose Status line already says implemented and merged, so the files reflect reality.

Evidence: `.scratch/session-report/issues/02-quality-movers-sections.md`, `.scratch/sub-frames/issues/01-viewer-opens-subs.md`, `02-solve-crossmatch-subs.md`, `03-stack-subs.md`, `.scratch/transit-fit/issues/01-fit-core.md`, `.scratch/variable-scan/issues/01-scan-core.md`, `02-candidate-view.md`, `.scratch/observing/issues/05-stale-session-dialog.md`, `.scratch/photometry/issues/05-picking-real-seestar-frames.md`, `.scratch/offline-analysis/issues/01-offline-mode-core.md`, `02-offline-analysis-views.md`, `03-recent-projects.md` — all show "Status: implemented in <hash> (merged)" yet `- [ ]` on every acceptance line. The code demonstrably exists and the matching tests pass (verified by the audit: e.g. 527 passed across the targeted files).

**Blocked by:** None (can start immediately)

**Status:** completed — reviewed (approved) and merged to main in 86d6892

- [x] Every listed issue file has its acceptance boxes checked (or a pointer to the implementing commit's test list) matching its Status line
- [x] No code changes; docs-only ticket

## Comments

Audit finding (kind: incorrect-spec-claim, severity: low; systemic doc-hygiene pattern).

Implemented in 4a8d860 (docs-only): all 12 listed issue files now have every acceptance box checked (`- [ ]` -> `- [x]`, 49 boxes total) and the "(implementor fills in)" comment replaced with a pointer to the implementing commit named in the Status line plus its test list. No code or test files touched.

Verification (branch `wt/app-improvement-17`, worktree venv with gi enabled so gi-gated widget suites run):

- Targeted tests — union of the implementing commits' test files (35 files, e.g. `tests/test_report_sections.py`, `tests/test_sub_frames_*.py`, `tests/test_stack_subs.py`, `tests/test_transit_fit.py`, `tests/test_variable_scan*.py`, `tests/test_observing_widgets.py`, `tests/test_photometry_picking.py`, `tests/test_offline_*.py`, `tests/test_recent*.py`, ...): `uv run pytest <files> -q -n auto` -> **785 passed, 5 failed**. All 5 failures are pre-existing on unmodified main (branch was identical to main before this ticket; 4 of 5 also fail in the main checkout): `test_report_sections.py::test_movers_section_omitted_when_no_moving_objects_package` (order-sensitive, passes in isolation), `test_observing_widgets.py::test_stale_session_dialog_resume_button_resumes_the_session`, `test_observing_widgets.py::test_stale_session_dialog_close_button_closes_and_starts_new`, `test_sub_frames_analysis_widgets.py::test_star_picker_lists_sub_and_row_click_opens_it`, `test_offline_analysis_widgets.py::test_photometry_run_button_measures_the_stored_session_offline`. Not caused by, and not fixable within, this docs-only ticket.
- `uv tool run ruff check` -> clean ("All checks passed!", exit 0).
- `uv tool run ty check` -> 108 diagnostics, byte-identical on unmodified main (exit 1 there too): all `gi.repository` missing-module-member in widget files, an artifact of gi being importable in the desktop venv (the pyproject override only silences import-not-found for the gi-less sandbox). This docs-only diff introduces none.
