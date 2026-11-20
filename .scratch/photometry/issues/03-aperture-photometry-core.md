# 03: Aperture photometry core

**What to build:** the pure measurement engine — given a sub-frame (FITS) and star picks with aperture settings, run aperture photometry (photutils) with sky annulus background, compute instrumental mags, the comp ensemble mean per frame, differential mag = target − ensemble, and propagate errors. Demoable: synthetic FITS with a sinusoidally varying target and constant comps recovers the injected amplitude.

**Blocked by:** seestar-link 01 (uv project scaffold)

**Status:** implemented in 7ca0be2

- [x] Circular aperture + sky annulus photometry per picked star
- [x] Comp ensemble = mean comp mag per frame; differential mag = target − ensemble
- [x] Error = sqrt(target_err² + mean(comp_err²))
- [x] JD taken from FITS header DATE-OBS
- [x] Tests: synthetic FITS, known fluxes → recovered differential mags within tolerance; ensemble math unit tests
