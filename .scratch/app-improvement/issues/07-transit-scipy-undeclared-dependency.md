# 07: transit fit imports scipy, which is not a declared dependency

**What to build:** declare `scipy` as an explicit project dependency so the transit fit cannot break at import time if the transitive provider (photutils) changes.

Evidence: `src/seestar_controller/transit/fit.py:60` `from scipy.optimize import least_squares`; `pyproject.toml` dependencies list astropy, cryptography, matplotlib, numpy, photutils, pillow, reportlab — no scipy (present only transitively via photutils in uv.lock). The transit-fit issue 01 acceptance even says "scipy/numpy".

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] `scipy` appears in `pyproject.toml` dependencies and `uv.lock` (`uv add scipy` or manual + `uv lock`)
- [ ] `uv run pytest tests/test_transit_fit.py -q` passes; full suite green; ty + ruff clean

## Comments

Audit finding (kind: bad-implementation / packaging, severity: medium).
