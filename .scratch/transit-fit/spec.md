# Spec: transit-fit

Status: completed 2026-11-20 (issue 01 implemented in b0628df)

## Problem Statement

When the target is an exoplanet host (or an eclipsing binary), the user wants to fit a transit model to the folded lightcurve, not just eyeball it — getting period, depth, duration, and mid-time with uncertainties.

## Solution

A transit-fit module: take the project lightcurve (and the period from period-analysis), fit a Mandel-Agol transit model (batman-style) with least-squares/MCMC-lite, show the model overplotted on the folded lightcurve with residuals, and save the parameters in the project.

## User Stories

1. As an observer, I want to fit a transit model to my folded lightcurve, so that I get quantitative parameters.
2. As an observer, I want to adjust period, mid-time, depth/duration (or radius ratio + impact parameter), so that I explore the fit.
3. As an observer, I want the model overplotted with residuals, so that I judge the fit.
4. As an observer, I want parameter uncertainties, so that I know the precision.
5. As an observer, I want the fit parameters saved in the project and exportable as CSV/JSON, so that they persist.

## Implementation Decisions

- Model: Mandel-Agol via `batman` if installable, else a small analytic implementation; fit with scipy least_squares, uncertainties from the covariance matrix.
- Input: folded lightcurve from period-analysis; free params: period (optional fixed), t0, Rp/Rs, a/Rs, inclination (or depth+duration parameterization — open question).
- Module boundary: pure Python (lightcurve + params → model + fit); view renders.

## Testing Decisions

- Good test = synthetic lightcurve with an injected Mandel-Agol transit of known parameters: recovered depth/duration/t0 within tolerance; residuals flat.
- Prior art: period-analysis Lomb-Scargle tests.

## Out of Scope

- Full MCMC, limb darkening fitting beyond fixed coefficients, TTV analysis.

## Further Notes

- Depends on period-analysis, photometry, project-store.

## Open Questions

- Parameterization: physical (Rp/Rs, a/Rs, i) vs simple (depth, duration, t0) — propose physical with sensible defaults.
- batman as a new dependency, or hand-rolled Mandel-Agol — propose batman if pip-installable cleanly, else hand-rolled.
