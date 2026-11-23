# Issue 19: sky annulus never range-checked against the image border — edge stars get truncated-annulus background bias (comp scatter up to 0.083 robust std, >1 mag outliers)

**Status:** completed (reviewed APPROVE, merged to main: annulus-outside-image pick-level skip in measure_star)

## Found while
Headless lightcurve run 2026-11-22 (vw-cep): comp stars showed robust scatter A 0.045 / B 0.039 / C 0.083 / D 0.045 with worst frames deviating >1 mag. Comps near the frame edge get a truncated sky annulus (8–12 px) → biased background → biased flux. `measure_star` does not check that the annulus fits inside the image.

## Expected
- `measure_star` skips (or flags) a pick whose sky annulus is not fully inside the image, with a distinct skip kind (e.g. `annulus-outside-image`); alternatively computes background from the valid annulus fraction and inflates mag_err.
- Tests: star 10 px from the edge → pick-level skip/flag, not silent biased flux.

## Scope
`src/seestar_controller/photometry/measure.py` (`measure_star`), `run.py` (kind aggregation), tests.
