# Issue 13: re-running photometry duplicates measurements — no upsert/dedup on write or read; CSV contains conflicting same-jd rows

**Status:** completed (reviewed APPROVE, merged to main: per-frame upsert + run-start clear + read-side dedup; re-runs are idempotent)

## Found while
Headless lightcurve run 2026-11-22 (project vw-cep, session 20261122T192149035964Z): after a second `run_session`, the db held 479 measurement rows over 384 distinct frames — 53 frames with 2–5 rows; the CSV had jd 2461367.410125379 four times with conflicting mags (3.29, −2.23, −2.39, …). `add_measurement` appends unconditionally.

## Expected
- `add_measurement` (or `run_session`) upserts per (frame_id) — or at least per (frame_id, pick-ensemble) — so a re-run replaces the previous run's rows instead of appending. `lightcurve()`/CSV export then never show duplicate jd rows.
- Alternatively a "clear previous run's measurements for this session" step at run start, recorded on the report.
- Tests: run twice → same row count; CSV has unique jd per frame.

## Scope
`src/seestar_controller/store/measurements.py` (add_measurement / lightcurve), `photometry/run.py` (run start), `store/csv_export.py`, tests in `tests/test_*measurements*.py` / `tests/test_photometry_run*.py`.
