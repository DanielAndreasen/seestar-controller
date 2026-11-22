# 15: NaN slips past the positivity guards in `absolute_g` / `abs_g_error`, returning NaN instead of None

**What to build:** make the H-R diagram magnitude helpers reject non-finite parallax like they reject non-positive parallax.

Evidence: `src/seestar_controller/hr_diagram/core.py:200-203` (`float(g)` with no `isfinite` check; `parallax_mas <= 0` is False for NaN) and `hr_diagram/plot.py:149-153` (same guard). Reproduced: `absolute_g(10.0, float('nan'))` → `nan`; `abs_g_error(parallax=nan, err=0.1)` → `nan`. Both functions document "None when not positive"; NaN is not positive. The normal pipeline is protected only because `hr_points` pre-filters via `_finite` (core.py:155-164) — direct callers get NaN. `tests/test_hr_diagram.py:198-209` covers None/0/negative but not NaN.

**Blocked by:** None (can start immediately)

**Status:** implemented in b5ade66 (pending review)

- [x] `absolute_g` / `abs_g_error` (and the plot.py twin) return None for NaN/inf parallax (`math.isfinite(p) and p > 0`), with NaN test cases added
- [x] `uv run pytest tests/test_hr_diagram.py -q` passes; full suite green; ty + ruff clean

## Comments

Audit finding (kind: bad-implementation, severity: low).

Implemented in b5ade66 on wt/app-improvement-15: `absolute_g` (core.py) and `abs_g_error` (plot.py) now return None unless `math.isfinite(parallax) and parallax > 0`; NaN/±inf parallax test cases added in tests/test_hr_diagram.py (absolute_g) and tests/test_hr_diagram_view.py (abs_g_error). Results: `uv run pytest tests/test_hr_diagram.py -q` 13 passed; `uv run pytest tests/test_hr_diagram_view.py tests/test_hr_diagram_view_widgets.py -q` 19 passed, 1 skipped (no gi in this worktree's venv — the gi-gated widget file was additionally run with the gi-capable main-checkout interpreter: 10 passed); `uv tool run ty check` clean; `uv tool run ruff check` clean. Full suite not run here (coordinator runs it at merge time).
