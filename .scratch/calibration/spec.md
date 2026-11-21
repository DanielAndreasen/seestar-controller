# Spec: calibration (image calibration: darks / flats / bias)

Status: ready-for-agent (0 issues)

## Problem Statement

The user wants image calibration (darks, flats, bias) in the desktop app's analysis. Reality of the hardware: the Seestar S50 takes darks automatically (before autofocus) and flats manually in the phone app, and applies both to every sub on the telescope — downloaded FITS subs are already calibrated, and there is no raw uncalibrated output mode. Bias frames are not taken separately (darks include the bias offset). Since the 2026-10 firmware, master darks are stored on the device in `darklibrary/<exp>s/` and can be downloaded. Re-applying darks to Seestar subs would double-subtract and ruin photometry.

## Solution

An image-calibration capability in the offline analysis, built around "calibrate only what is actually uncalibrated":

1. **Calibration-frame library**: download the device's `darklibrary/` (master darks per exposure time) over the existing file link into the project (or a shared library location); detect already-calibrated Seestar subs (header/behavior heuristics) and never re-apply darks/flats to them.
2. **Master frame building**: build master dark / master flat / master bias from user-supplied calibration frames (median combine, sigma clipping) — usable for external/uncalibrated data and for synthetic flats.
3. **Calibration application**: apply master dark/flat/bias to frames that are detected as uncalibrated, in the image-enhance and photometry pipelines, with per-frame provenance recorded (which master was applied, when).
4. **Synthetic flats**: generate a synthetic flat (e.g. radial vignetting + dust motes model) as a fallback when no real flats exist.
5. **UI**: a Calibration view per project — shows device dark library status, lets the user build/import master frames, toggle application per pipeline, and shows which frames were calibrated by the scope vs. by us.

## User Stories

1. As a user, I want the app to download the Seestar's dark library (`darklibrary/`) into my project, so that I have the scope's master darks available offline.
2. As a user, I want the app to detect that Seestar subs are already dark/flat calibrated on-device, so that darks are never double-applied.
3. As a user, I want to build a master dark/flat/bias from a set of calibration frames (median combine), so that I can calibrate uncalibrated data.
4. As a user, I want the photometry pipeline to apply calibration only to frames that need it, so that my measurements stay correct.
5. As a user, I want a synthetic flat option when no real flats exist, so that vignetting/dust can still be corrected.
6. As a user, I want to see in the UI which frames were calibrated by the scope and which by the app, so that I trust the pipeline.
7. As a user, I want calibration provenance recorded per frame in the project store, so that analysis is reproducible.

## Implementation Decisions

- Pure `calibration/` package (no gi): frame-kind detection (IMAGETYP/header heuristics + known Seestar behavior), master-frame building (astropy/numpy median + sigma clip), application (dark subtract, flat divide, bias subtract), synthetic flat model, provenance model.
- Link: read-only download of `darklibrary/` via existing file-list/get link methods; no new wire commands needed for v1.
- Store: calibration provenance per frame + master-frame registry in project store (schema bump + migration per project-store 07 pattern).
- Pipelines: image-enhance and photometry call into `calibration.apply_if_needed(frame)`; default = never touch Seestar subs.
- GUI: Calibration view (library status, build master, toggles, provenance list); pure logic separated from widgets per ui-shell pattern.

## Testing Decisions

- Pure tests: detection heuristics on synthetic headers, master-frame math on synthetic arrays (known dark/flat injected, recovered within tolerance), application correctness, provenance recording, schema migration — no gi, no network.
- Mock-scope tests: mock serves a fake `darklibrary/` file list + files; download flow tested against mock.
- Widget tests via ui harness; full suite stays green.

## Out of Scope

- Capturing raw uncalibrated frames from the scope (not supported by hardware/firmware).
- Driving scope-level calibration (leveling/alignment) from the desktop — done with the phone app at the scope.
- Re-applying darks/flats to already-calibrated Seestar subs (would corrupt data).

## Further Notes

- Depends on: seestar-link (file download), project-store (provenance + schema), image-enhance + photometry (pipeline hooks), projects-view/offline-analysis (UI placement).
- Sources: r/seestar "Calibration frames" threads (darks before autofocus, flats manual in app, applied on-device); mjm1138/m110 PR #169 (darklibrary/<exp>s/ on device since 2026-10 firmware, subs already dark-subtracted, never re-apply); naztronaut/siril-scripts #121/#122 (darklibrary usable, lights already calibrated).
