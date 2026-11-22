# 06: Gaia cross-match is a pure-Python O(detections × sources) loop — minutes at documented defaults

**What to build:** make `crossmatch` finish in seconds on a dense Seestar field (thousands of detections × up to 100k Gaia rows) by evaluating only pairs within the match radius.

Evidence: `src/seestar_controller/gaia/matching.py:166-172` nested loop computing `angular_distance` for every detection×Gaia pair in pure Python; `gaia/query.py:82-83` DEFAULT_MAXREC=100_000. Measured: `crossmatch(200 detections, 20000 gaia rows)` took 6.85 s → ~3 min at defaults. `tests/test_gaia_crossmatch.py` has only 8 small-input tests, no scale test. This runs on every plate-solve/cross-match with a cold cache and blocks the worker thread.

**Blocked by:** None (can start immediately)

**Status:** completed — reviewed (approved) and merged to main in e9aa2a8

- [x] `crossmatch` of 2000 detections × 100k Gaia rows completes in a few seconds (add a scale/perf test with a generous bound)
- [x] Match results are identical to the brute-force result on the existing test fixtures (radius semantics unchanged)
- [x] `uv run pytest tests/test_gaia_crossmatch.py tests/test_gaia_overlay.py -q` passes; full suite green; ty + ruff clean

## Comments

Implemented in 9e5d673 on `wt/app-improvement-06`. `crossmatch` now builds a Dec-sorted band index over the Gaia rows (`matching._candidate_pairs`): each detection scans only the Dec band `[dec_d − r, dec_d + r]`, an RA lower bound `2·asin(cos(dec_max)·|sin(Δra/2)|)` (RA-wrap invariant, pole-clamped) rejects inside the band, and only survivors get the exact `angular_distance`; both bounds carry a tiny FP slack so the candidate set — and hence the greedy one-to-one result — is bit-identical to the old brute-force loop. Radius semantics unchanged; the 8 existing fixture tests pass unmodified.

New tests in `tests/test_gaia_crossmatch.py`: `test_crossmatch_is_identical_to_the_brute_force_loop_on_random_fields` (randomized differential test vs a brute-force reference: ordinary field, RA 0/360 wrap, near-pole full-RA field, radii 0.4–30 arcsec) and `test_crossmatch_scale_2000_detections_x_100k_gaia_rows_is_fast` (2000 × 100k in 0.39 s measured; generous 10 s bound — brute force needs ~5 min at these sizes).

Results: `uv run pytest tests/test_gaia_crossmatch.py tests/test_gaia_overlay.py -q` → 28 passed (with the gi-enabled main venv via `UV_PROJECT_ENVIRONMENT`; the worktree-local venv lacks gi). All other gaia files (`test_gaia_boundary/detect/pipeline/persistence/query/overlay_widgets`) → 56 passed. `uv tool run ty check` → All checks passed. `uv tool run ruff check` → All checks passed.
