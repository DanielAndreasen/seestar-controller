# App Improvement — audit-driven fixes

**Status:** completed — all 21 issues implemented, reviewed (approved) and merged to main

Whole-application audit effort (read-only testing + spec/issue verification). Findings are filed as individual tickets under `.scratch/app-improvement/issues/`.

## Problem Statement

A full audit of the application (test suite runs, code review of every package, and verification of every existing spec/issue under `.scratch/` against the code) found that the app is in good shape overall — 2808 of 2823 tests pass — but a set of real defects remain:

1. Three user-facing GUI flows are broken whenever GTK is actually present (the widget test suites only "pass" in headless sandboxes because they skip there): generating/exporting a session report always fails, running offline photometry from the results panel is a no-op, and the mover overlay shows a blank frame counter on first selection. Two more views have latent state bugs (stale-session dialog never forgotten; a capture watermark that can silently drop a frame forever).
2. The test suite has 13 failures in any environment where `gi` is importable, caused by a mix of those src bugs and test-side bugs (GTK3-only API calls, property-called-as-method, order-dependent module assertions).
3. Performance: the Gaia cross-match is a pure-Python O(detections × sources) loop that takes minutes at documented defaults.
4. Packaging: the transit fit imports scipy, which is only present transitively.
5. Spec/issue hygiene: several specs and issue files make claims that are false or stale (unchecked acceptance boxes on merged work, outdated status lines, a wrong "migration-free" claim, an overpromised resolver fallback, stale "known failures" notes).
6. Two promised features have no implementation (deleting projects; opening a stitched mosaic in the enhance view).

## Solution

Fix each finding as its own small ticket (see `issues/`): repair the broken GUI flows and their tests, fix the test-side GTK4 API misuse, close the small robustness gaps in link/store/capture/hr-diagram, vectorize or index the cross-match, declare scipy explicitly, implement or descope the two missing features, and correct the stale spec/issue/docs text. No new features beyond what existing specs already promise; no rewrites of healthy modules.

## User Stories

1. As a user with a desktop display, I want the session report "Generate" and "Export PDF" buttons to actually write the report files, so that I can keep a record of the night.
2. As a user working offline, I want the photometry Run button to measure the stored session without me having to click Refresh first, so that offline analysis works end to end.
3. As a user, I want the mover overlay to show the frame counter ("frame 1/6") as soon as a session is selected, so that the blink controls make sense.
4. As a user, I want the stale-session dialog to be dismissed reliably after I choose resume or start-new, so that the dialog does not linger.
5. As a user, I want a sub-frame that failed to store during capture to be retried on a later poll, so that a transient disk/DB error does not silently lose data for the night.
6. As a user plate-solving a dense field, I want the Gaia cross-match to finish in seconds, not minutes, so that the overlay stays interactive.
7. As a maintainer, I want the full test suite green in environments where GTK is importable, not only in headless ones, so that "full suite green" is a meaningful claim.
8. As a maintainer, I want every declared import to be a declared dependency, so that a leaner install cannot break the transit fit at import time.
9. As a user managing projects, I want to delete an old project from the app, so that the projects list does not accumulate dead entries (as promised by the project-store spec).
10. As a user, I want to open a stitched mosaic in the enhance view, so that I can stretch/denoise it like any other stack (as promised by the mosaic-stitch spec).
11. As a maintainer, I want spec and issue files to match reality (checked boxes on merged work, current status lines, correct failure lists), so that triage does not re-do finished work or trust stale claims.
12. As a maintainer, I want `timeout=0` and per-call timeouts on the link to be honored, so that the request contract is predictable.
13. As a maintainer, I want `create_project` to not leave a poisoned half-created project directory behind on failure, so that the project name stays usable.
14. As a user of the H-R diagram helpers, I want NaN parallaxes to be rejected like non-positive ones, so that bad rows are dropped instead of propagated.

## Implementation Decisions

- Each finding is one ticket under `.scratch/app-improvement/issues/`, numbered from 01; all are independent (`Blocked by: None`) except where noted in the ticket.
- GUI fixes follow the existing in-repo pattern: store access must happen on the db-owning thread (marshal via `invoke_on_main`/`GLib.idle_add`, as `RunController` already does) — the session report panel must not call `generate_report`/`generate_pdf` on a worker thread with the main-thread sqlite connection.
- View fixes follow the existing sibling-view patterns: refresh dropdowns inside `_render` when the snapshot's session list changes (as the mover overlay and frame-quality views do), guard re-entrant `_render` against `notify::selected` recursion, and clear dialog state in button callbacks rather than relying solely on the asynchronous "closed" signal.
- Test fixes follow the repo's documented GTK4 conventions: buttons are triggered with `.emit("clicked")`, teardown via the view's own destroy handler, properties are accessed without parentheses.
- The cross-match fix should bucket/index Gaia rows (or vectorize with numpy) so only pairs within the match radius are evaluated; keep the public behavior and add a scale test.
- `scipy` becomes an explicit dependency in `pyproject.toml` (it is already in the lock via photutils).
- Missing features: either implement `store.delete_project` (with UI) under a ticket, or move the user story to Out of Scope and align the two specs; same for opening a stitched mosaic in the enhance view (register the mosaic FITS as a frame, or add an open-file path to the viewer).
- Docs fixes are text-only edits to the named spec/issue/wiki files; no code changes.
- No changes to healthy modules; no test weakening (per AGENTS.md, known failures must not be "fixed" by weakening tests).

## Testing Decisions

- A good test here tests external behavior: the GUI flow produces a file / stores measurements / shows the label; the API honors its documented contract. No implementation-detail assertions.
- Every fixed widget behavior needs its gi-gated widget test to pass in a gi-present environment (`uv run pytest tests/<file> -q`), and the full suite must stay green: `uv run pytest -q -n auto`.
- Prior art: the sibling widget suites (`test_gaia_overlay_widgets.py`, `test_astrometry_view_widgets.py`, `test_sub_frames_analysis_widgets.py` docstrings) document the GTK4 conventions the broken tests violate; `photometry/run.py` documents the store thread-affinity marshalling the report panel must copy.
- Run lint/typecheck before committing each ticket: `uv tool run ty check` and `uv tool run ruff check` (per AGENTS.md).

## Out of Scope

- New features not promised by any existing spec.
- Reworking healthy packages (link protocol, store schema, analysis numerics all audited clean).
- Hardware verification against a real Scope (mock only, as throughout the project).
- The two documented no-gi sandbox failures are not to be "fixed" by weakening tests; they are addressed by making the suite robust in both environments.

## Further Notes

- Environment quirk discovered by the audit: this venv now has a working `gi` (dependency-upgrade issue 06), so the gi-gated widget suites run instead of skipping and expose 13 failures. `docs/wiki/dependency-upgrade.md` and AGENTS.md still describe the no-gi sandbox behavior — see the docs ticket.
- Audit method: 7 parallel read-only subagents (3 verifying existing specs/issues against code, 3 reviewing src packages, 1 root-causing the 13 failing tests), plus a full-suite run. All findings carry file:line or test-id evidence in their ticket.
