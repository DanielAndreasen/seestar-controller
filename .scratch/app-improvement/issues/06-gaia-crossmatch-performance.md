# 06: Gaia cross-match is a pure-Python O(detections × sources) loop — minutes at documented defaults

**What to build:** make `crossmatch` finish in seconds on a dense Seestar field (thousands of detections × up to 100k Gaia rows) by evaluating only pairs within the match radius.

Evidence: `src/seestar_controller/gaia/matching.py:166-172` nested loop computing `angular_distance` for every detection×Gaia pair in pure Python; `gaia/query.py:82-83` DEFAULT_MAXREC=100_000. Measured: `crossmatch(200 detections, 20000 gaia rows)` took 6.85 s → ~3 min at defaults. `tests/test_gaia_crossmatch.py` has only 8 small-input tests, no scale test. This runs on every plate-solve/cross-match with a cold cache and blocks the worker thread.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] `crossmatch` of 2000 detections × 100k Gaia rows completes in a few seconds (add a scale/perf test with a generous bound)
- [ ] Match results are identical to the brute-force result on the existing test fixtures (radius semantics unchanged)
- [ ] `uv run pytest tests/test_gaia_crossmatch.py tests/test_gaia_overlay.py -q` passes; full suite green; ty + ruff clean

## Comments

Audit finding (kind: bad-implementation, severity: medium). Fix direction: bucket Gaia rows into a coarse RA/Dec grid (or vectorize the separation computation with numpy) so only pairs within `radius_arcsec` are evaluated; alternatively cap maxrec on the interactive overlay path.
