# Issue 15: mag_err badly underestimated — median 0.006 vs actual 0.30 mag scatter

**Status:** needs-triage

## Found while
Headless lightcurve run 2026-11-22 (vw-cep): mag_err median 0.0061 / max 0.0090, while the target differential curve has robust scatter 0.30 mag. The error model (`MAG_ERR_COEFF` etc. in measure.py) ignores centroid/refinement uncertainty, edge systematics at small aperture radius (r=1.5 px here), and WCS pointing error.

## Expected
- mag_err includes at least: Poisson + read-noise terms (existing), aperture-radius/centroid refinement term, and a term for the reprojected-pick offset (pick_offset) or WCS rms when available.
- Sanity test: synthetic frames with known noise → mag_err within a factor ~2 of the empirical scatter across frames.

## Scope
`src/seestar_controller/photometry/measure.py` (error model), tests in `tests/test_photometry_measure*.py`.
