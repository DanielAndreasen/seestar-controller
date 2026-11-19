# Spec: period-analysis

Status: ready-for-agent

## Problem Statement

From the accumulated lightcurve of a variable star, the user wants to find its period and fold the whole lightcurve by that period, to confirm the variability and see the full phase coverage.

## Solution

A period-analysis module: plot the project's lightcurve (mag vs time, sessions distinguished), compute a Lomb-Scargle periodogram (astropy) over a frequency grid derived from the baseline, let the user pick a peak or enter a period manually, and render the phase-folded lightcurve covering the full dataset. Export CSV.

## User Stories

1. As an observer, I want to see the lightcurve of my project (mag vs JD), with sessions colored differently, so that I see night-to-night coverage.
2. As an observer, I want error bars on each point, so that I judge data quality.
3. As an observer, I want to run a Lomb-Scargle periodogram and see the power spectrum, so that I can find candidate periods.
4. As an observer, I want to click a peak in the periodogram to select that period, so that finding the period is visual.
5. As an observer, I want to enter a period manually (days), so that I can use a known period.
6. As an observer, I want the phase-folded lightcurve (mag vs phase 0–1) covering all sessions, so that I see the full folded curve.
7. As an observer, I want to zoom/pan both plots, so that I inspect details.
8. As an observer, I want the chosen period saved in the project, so that it persists.
9. As an observer, I want to export the lightcurve (and folded data) as CSV, so that I can analyze elsewhere.
10. As an observer, I want to mark/remove outlier points, so that they don't distort the periodogram.

## Implementation Decisions

- Periodogram: astropy.timeseries.LombScargle; frequency grid from min ~ 2 cycles over baseline to Nyquist-ish of median cadence; show top N peaks.
- Fold: phase = ((jd − jd0) / period) mod 1; jd0 configurable (default first observation); plot phase 0–1.
- Plots: matplotlib embedded in GTK (FigureCanvasGTK4Agg) — pragmatic and good-looking; alternative pure-Cairo if embedding proves heavy.
- Data: measurements from project-store; outliers flagged in DB (excluded flag).
- CSV export: jd, utc, mag, mag_err, session, target (+ phase when folded).
- Module boundary: analysis pure Python (series → periodogram/fold); view renders.

## Testing Decisions

- Good test = synthetic sinusoid (known period + noise + gaps): Lomb-Scargle recovers the period within tolerance; manual fold produces correct phases; CSV round-trips; outlier exclusion changes result as expected.

## Out of Scope

- MCMC period fitting, harmonic/multi-period models, O-C diagrams, ephemeris prediction beyond folding.

## Further Notes

- Depends on project-store and ui-shell.
