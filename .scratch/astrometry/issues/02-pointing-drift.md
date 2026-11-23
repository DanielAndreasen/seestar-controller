# 02: Pointing error + drift statistics (pure)

**What to build:** per-session pointing accuracy and drift: compare the session's requested target RA/Dec (observing queue entry) against the solved WCS center -> pointing error; across the session's solved subs, compute RA/Dec offset vs. time -> drift series for a drift plot. Demoable: synthetic session with injected offsets -> pointing error and drift math correct.

**Blocked by:** astrometry 01, observing queue (done)

**Status:** implemented in a6f531a (merged)

- [x] Pure functions: `pointing_error(project, session_id)` (requested vs solved center, arcsec) and `drift_series(project, session_id)` (offset vs frame time)
- [x] Persisted/derived from stored WCS per frame (astrometry 01) + queue entry coords
- [x] Tests: injected offsets -> correct error/drift within tolerance; missing WCS frames skipped with count reported
- [x] Full suite green

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `a6f531a`; test list: tests/test_astrometry_drift.py, tests/test_astrometry_boundary.py. Re-verified during the issue-17 audit: all referenced commits exist on main and the listed targeted tests pass (`uv run pytest <files> -q -n auto`).
