# 15: NaN slips past the positivity guards in `absolute_g` / `abs_g_error`, returning NaN instead of None

**What to build:** make the H-R diagram magnitude helpers reject non-finite parallax like they reject non-positive parallax.

Evidence: `src/seestar_controller/hr_diagram/core.py:200-203` (`float(g)` with no `isfinite` check; `parallax_mas <= 0` is False for NaN) and `hr_diagram/plot.py:149-153` (same guard). Reproduced: `absolute_g(10.0, float('nan'))` → `nan`; `abs_g_error(parallax=nan, err=0.1)` → `nan`. Both functions document "None when not positive"; NaN is not positive. The normal pipeline is protected only because `hr_points` pre-filters via `_finite` (core.py:155-164) — direct callers get NaN. `tests/test_hr_diagram.py:198-209` covers None/0/negative but not NaN.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] `absolute_g` / `abs_g_error` (and the plot.py twin) return None for NaN/inf parallax (`math.isfinite(p) and p > 0`), with NaN test cases added
- [ ] `uv run pytest tests/test_hr_diagram.py -q` passes; full suite green; ty + ruff clean

## Comments

Audit finding (kind: bad-implementation, severity: low).
