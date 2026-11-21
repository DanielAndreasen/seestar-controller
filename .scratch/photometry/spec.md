# Spec: photometry

Status: completed 2026-11-21 — all 5 issues implemented and reviewed (05: 8f00932+a2e094b)

## Problem Statement

The user wants to do real science with the S50: differential photometry of a variable target star against standard comparison stars, measured from the individual sub-frames, saved into the project so brightness can be monitored across nights.

## Solution

A photometry module: pick the target star and one or more comparison stars by clicking on the preview/stack (with catalog assist to find good comps), run aperture photometry on each sub-frame of a session, compute differential magnitudes against the comp ensemble, and store measurements (JD, mag, mag_err) in the project store.

## User Stories

1. As an observer, I want to click the target star on the live preview or a stack, so that I select it without entering coordinates.
2. As an observer, I want to click one or more comparison stars the same way, so that I build the comp ensemble.
3. As an observer, I want catalog assist to suggest good standard stars near the target, so that I pick scientifically sound comps.
4. As an observer, I want to see all picked stars marked on the image with apertures, so that I verify the selection.
5. As an observer, I want to adjust aperture radius (and inner/outer sky annulus), so that the measurement is optimal.
6. As an observer, I want to run photometry over all subs of a session, so that I get one measurement per frame.
7. As an observer, I want differential magnitude = target − comp ensemble per frame, with error propagation, so that the result is scientifically valid.
8. As an observer, I want per-frame results viewable (table + scatter), with outlier flagging, so that I can spot bad frames.
9. As an observer, I want measurements saved into the project lightcurve, so that nights accumulate.
10. As an observer, I want star picks saved in the project, so that I reuse them next night.
11. As an observer, I want click position converted to sky coordinates via the FITS WCS, so that picks can be matched to catalog stars.

## Implementation Decisions

- Aperture photometry: photutils `aperture_photometry` with circular aperture + sky annulus; background subtracted per aperture.
- Comp ensemble: mean magnitude of all comps per frame; differential mag = target_mag − ensemble_mag; error = sqrt(target_err² + mean(comp_err²)).
- Flux→mag: instrumental mag via -2.5*log10(flux); differential mags are calibration-free, which is the point.
- Time per frame: JD from FITS header (DATE-OBS), fallback to file mtime.
- Star picking: click → pixel coords; WCS → RA/Dec; catalog assist lists standard stars within a radius, sorted by magnitude proximity to target.
- Module boundary: measurement pipeline pure Python (frames + picks → measurements); view handles clicks and rendering.
- Sub-frames are the input (linear 10 s); stacks are never used for photometry.

## Testing Decisions

- Good test = synthetic FITS with stars of known flux (target varying sinusoidally, comps constant): recovered differential mags match the injected amplitude within tolerance; ensemble math correct; WCS click→sky conversion round-trips.
- Outlier flagging test with one injected bad frame.

## Out of Scope

- PSF-fit photometry, calibration frames (the scope applies darks internally), airmass/extinction correction, transit modeling.

## Further Notes

- Depends on catalog, project-store, ui-shell; consumes subs downloaded by observing/seestar-link.
