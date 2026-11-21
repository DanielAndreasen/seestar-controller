# 01: Transit-fit core — Mandel-Agol-style model + least-squares fit (pure)

**What to build:** the pure (no-gi) core of transit-fit: take the project's folded lightcurve (phase + mag + err from period-analysis) and fit a transit model — analytic Mandel-Agol-style trapezoid/box model implemented in numpy (avoid the batman dependency; a small analytic transit model is fine for this scope), free params t0, depth, duration (period fixed from the project, optionally refit), fit with scipy.optimize.least_squares, uncertainties from the covariance matrix. Save fit parameters in the project config (project.json, no schema bump) and exportable as JSON. Demoable: synthetic folded lightcurve with an injected transit -> fit recovers depth/duration/t0 within uncertainties.

**Blocked by:** None (period-analysis, project-store done)

**Status:** implemented in b0628df (merged)

- [x] Pure `transit/` package, no gi (subprocess boundary test); scipy/numpy; analytic transit model (no batman dependency)
- [x] `transit_model(phase, t0, depth, duration)` -> model mags; `fit_transit(phases, mags, errs, period, ...)` -> FitResult(params, errors, residuals, chi2) via scipy least_squares + covariance
- [x] Parameters saved in project config (project.json) + JSON export; no store schema bump
- [x] Tests: synthetic injected transit -> recovered params within uncertainties; fixed vs free period; persistence round-trip; no-gi boundary; no network
- [x] Full suite green

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `b0628df`; test list: `tests/test_transit_fit.py` (added there).
