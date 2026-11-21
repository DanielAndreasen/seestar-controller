# Spec: frame-quality

Status: completed 2026-11-20 (01: 82ee1a9; 02: fbc5578; 03: 6637761)

## Problem Statement

Not all sub-frames are equal: clouds, focus drift, and tracking errors degrade some. The user wants per-frame quality metrics (FWHM, background, star count), automatic flagging/rejection of bad frames, and night-level trends (focus, sky brightness, airmass) to judge the session.

## Solution

A frame-quality module: compute FWHM, background level/std, and detected star count per sub-frame; flag outliers; plot focus trend, sky-quality (background) trend, and airmass vs. time per session; optionally exclude flagged frames from photometry and stacking.

## User Stories

1. As an observer, I want FWHM, background, and star count computed per sub-frame, so that I see frame quality.
2. As an observer, I want bad frames auto-flagged (outlier metrics), so that I can exclude them.
3. As an observer, I want to exclude flagged frames from photometry, so that the lightcurve is cleaner.
4. As an observer, I want a focus trend (FWHM vs. time) over the night, so that I see focus drift.
5. As an observer, I want a sky-quality trend (background vs. time), so that I see clouds/moonlight.
6. As an observer, I want an airmass curve for the session, so that I can correct or judge extinction.
7. As an observer, I want quality metrics saved in the project per session, so that they persist.
8. As an observer, I want a per-session quality summary (median FWHM, % rejected), so that I compare nights.

## Implementation Decisions

- Metrics: photutils DAOStarFinder for star count + FWHM estimate; background = median + std in an annulus/grid; airmass from target coords + time (astropy.coordinates.AltAz).
- Flagging: robust z-score (MAD) per metric per session; threshold configurable.
- Photometry integration: photometry run takes an optional excluded-frames set.
- Module boundary: pure Python (subs + coords → metrics table + trends); view renders plots.

## Testing Decisions

- Good test = synthetic subs with one injected blurry/cloudy frame: metrics flag it, others pass; airmass curve matches astropy AltAz within tolerance; exclusion removes exactly the flagged frame from photometry input.
- Prior art: photometry synthetic-FITS tests, period-analysis time-series tests.

## Out of Scope

- Automatic refocus, cloud prediction, full extinction correction of the lightcurve.

## Further Notes

- Depends on photometry, project-store, observing (session metadata), catalog 02 (coordinates).

## Open Questions

- Flag threshold default (e.g. MAD z > 3) — propose 3, configurable.
- Exclude flagged frames from the scope's own stacking too, or only from our photometry? Propose: our photometry only (scope stacking is out of our control).
