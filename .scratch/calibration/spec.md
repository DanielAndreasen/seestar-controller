# Spec: calibration

Status: needs-info

## Problem Statement

Raw Seestar frames carry fixed-pattern noise (hot pixels, vignetting, dust motes) and a per-frame bias/dark signal. Without calibration frames, photometry on stacked frames is systematically biased and image enhancement amplifies the artifacts. The user wants to run calibration (bias/dark/flat) before an observing night and have the app apply it consistently to science frames.

## Solution

A calibration module + GUI flow: capture or load calibration frames (bias, dark at relevant exposure/gain, flat), build master calibration frames per (exposure, gain, filter) configuration, store them in the project, and apply them to incoming subs/stacks — both in the live preview/enhance pipeline and when measuring photometry. Calibration state is shown in the Observe view so the user knows whether the current night is calibrated.

## User Stories

1. As a user, I want to capture a set of bias frames (shortest exposure, lens covered / shutter closed) from the GUI, so that the sensor's readout offset is characterized.
2. As a user, I want to capture dark frames at the exposure/gain I plan to use, so that thermal/hot-pixel signal can be subtracted.
3. As a user, I want to capture flat frames (e.g. against a uniformly lit surface or dawn sky), so that vignetting and dust motes can be divided out.
4. As a user, I want the app to build master bias/dark/flat per (exposure, gain) configuration (median combine, outlier rejection), stored in the project, so that calibration is reusable across nights.
5. As a user, I want to apply the matching master frames to each science sub automatically (dark/bias subtract, flat divide), so that photometry and enhancement work on calibrated data.
6. As a user, I want to see in the Observe view whether the current configuration has valid calibration, and a warning if it does not, so that I don't waste a night on uncalibrated data.
7. As a user, I want to re-run calibration when the sensor temperature or configuration changes, so that stale masters are not applied.
8. As a user, I want calibrated stacks saved alongside raw stacks in the project, so that raw data is never destroyed.

## Implementation Decisions

- Pure `calibration/` package (no gi): frame capture orchestration via the link Device (reuse stacking/frame-download machinery), master-frame building (median combine with sigma clipping, numpy), matching logic (nearest exposure/gain within tolerance), and application (subtract masters, divide flat, clip) on FITS data (astropy, lazy import as in link/frames.py).
- Storage: masters under `<project>/calibration/<kind>/<exposure>_<gain>.fits` + metadata in the project DB (schema bump, migration per project-store 07 pattern); calibrated outputs under the existing `enhanced/` or a new `calibrated/` kind dir — decide during implementation, keep raw untouched.
- Link: calibration capture reuses `start_stack`/`stop_stack` + frame download; no new wire methods assumed — verify against fw 9.75 compat shim (link/compat.py) and mock scope; live verification read-only/bad-param only.
- GUI: calibration section in the Observe view (or its own view if it grows): buttons Capture bias/dark/flat, progress, master list per config, "calibrated: yes/no" indicator feeding the Start-night enablement.
- Photometry: measurement pipeline applies calibration to subs before aperture photometry when a matching master exists; otherwise falls back to raw with a flag recorded in the measurement row.

## Testing Decisions

- Pure tests with synthetic FITS arrays (numpy): master combine correctness, outlier rejection, apply math (bias/dark subtract, flat divide, clipping), matching tolerance, missing-master fallback.
- Mock-scope tests for the capture flow (mock serves frames; firmware knob from link compat).
- No network, no gi in pure modules; widget tests via ui harness; full suite stays green.

## Out of Scope

- Dark optimization / dark scaling across temperatures beyond nearest-exposure matching; BPM (bad pixel map) as a separate product (can be derived from master dark later); spectroscopic calibration (wavelength solution) — the S50 is an OSC imager.

## Further Notes

- Depends on: project-store (storage), seestar-link (capture/download), observing (night flow), photometry (apply before measuring).
- Open question for the user: flat-field source preference (dedicated flat panel / dawn sky / white T-shirt over the aperture)?
