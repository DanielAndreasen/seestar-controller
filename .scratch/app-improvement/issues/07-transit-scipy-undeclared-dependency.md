# 07: transit fit imports scipy, which is not a declared dependency

**What to build:** declare `scipy` as an explicit project dependency so the transit fit cannot break at import time if the transitive provider (photutils) changes.

Evidence: `src/seestar_controller/transit/fit.py:60` `from scipy.optimize import least_squares`; `pyproject.toml` dependencies list astropy, cryptography, matplotlib, numpy, photutils, pillow, reportlab — no scipy (present only transitively via photutils in uv.lock). The transit-fit issue 01 acceptance even says "scipy/numpy".

**Blocked by:** None (can start immediately)

**Status:** completed — reviewed (approved) and merged to main in bbf25b0

- [x] `scipy` appears in `pyproject.toml` dependencies and `uv.lock` (`uv add scipy` or manual + `uv lock`)
- [x] `uv run pytest tests/test_transit_fit.py -q` passes; full suite green; ty + ruff clean

## Comments

Audit finding (kind: bad-implementation / packaging, severity: medium).

Implemented in 1c4e419: `uv add scipy` added `scipy>=1.18.1` to `pyproject.toml` dependencies (alphabetical, matching existing style) and marked scipy as a direct dependency of `seestar-controller` in `uv.lock` (specifier `>=1.18.1`; the locked scipy 1.18.1 package entry is unchanged, so no version bumps anywhere). Targeted tests: `uv run pytest tests/test_transit_fit.py -q` -> 17 passed. `uv tool run ty check` -> All checks passed! `uv tool run ruff check` -> All checks passed! Full suite not run here (coordinator runs it at merge time per ticket rules); the change is packaging-only, no code touched.
