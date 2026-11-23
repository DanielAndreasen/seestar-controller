# Issue 17: run_session leaves snapshot.session_id set to an invalid id after a failed run

**Status:** needs-triage

## Found while
Headless lightcurve run 2026-11-22: after `run_session("no-such-session")` raised `RunError`, `snapshot.session_id == "no-such-session"` with `report=None` — `_session_id` is assigned before the known-sessions validation in `RunController.run_session`.

## Expected
- A failed run must not mutate `snapshot.session_id`; the snapshot keeps the previous valid session (or None) and carries the error. Test: failed run → snapshot.session_id unchanged, snapshot.error set.

## Scope
`src/seestar_controller/photometry/run.py` (order of assignment vs validation), `tests/test_photometry_run*.py`.
