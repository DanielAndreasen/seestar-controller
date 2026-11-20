# Spec: calibration

Status: needs-info

## Problem Statement

Before observing, the SeeStar app performs an instrument calibration: leveling the scope (so the alt-az mount knows its horizontal plane) and a star alignment (plate-solve based position calibration, so goto accuracy is good). Without this, gotos land off-target and tracking drifts. The user wants the same flow available from this app before starting a night.

## Solution

A calibration flow in the app that mirrors the official Seestar app: (1) level the scope — drive the mount to level using the built-in routine and show live tilt readout until within tolerance; (2) star alignment — slew to one or more bright stars, capture a frame, plate-solve it locally to get the actual pointing, and feed the offset back so subsequent gotos are accurate. Calibration state (level OK, alignment stars done) is stored per session/project and shown in the Observe view, gating "Start night".

## User Stories

1. As a user, I want to level the scope from the app, with a live tilt/level indicator, so that the mount's horizontal reference is set before observing.
2. As a user, I want to run a star alignment: the app slews to a suggested bright star, captures and plate-solves a frame, computes the pointing offset, and repeats for N stars, so that goto accuracy improves.
3. As a user, I want to see alignment quality (RMS error, per-star residuals) after the procedure, so that I know whether to redo it.
4. As a user, I want the calibration state (level + alignment) shown in the Observe view and recorded in the session log, so that I know the night started from a calibrated mount.
5. As a user, I want "Start night" to warn (not hard-block) if level/alignment was not done this session, so that I can proceed deliberately.
6. As a user, I want the alignment to be repeatable mid-night (re-align on a star) if gotos start missing.

## Implementation Decisions

- Pure `calibration/` package (no gi): level-state parsing from device state (fw 9.75 `iscope_get_app_state` tree via link/compat.py — check what level/tilt fields exist; probe live read-only), alignment math (plate-solve result -> RA/Dec offset -> RMS), star suggestion (bright catalog stars near current pointing via catalog module), session recording via store/sessions.
- Plate solving: local solver (e.g. astrometry.net solve-field if available, or a lightweight WCS fit) — decide during implementation; must work offline. Fallback: use the scope's own reported position if the firmware exposes solved coordinates.
- Link: reuse Device facade (state/goto/start_stack/stop_stack/download_frames) through the fw-9.75 compat shim; any new wire methods (e.g. a dedicated level command) must be probed live read-only/bad-param only and added to link/compat.py with legacy fallback + mock support. Never send mutating commands with valid params to the real scope during development.
- GUI: calibration section in the Observe view (Level button + live indicator, Align button + star list + RMS readout, per-session status), wired into the night-start warning.
- Storage: calibration record per session in the project DB (schema bump + migration per project-store 07 pattern).

## Testing Decisions

- Pure tests: offset/RMS math, star suggestion filtering, level-state parsing from sample app-state trees, session recording — no gi, no network.
- Mock-scope tests for the full flow (mock serves frames + state; firmware knob "9.75"/"legacy"/"both" from link compat).
- Widget tests via ui harness; full suite stays green.

## Out of Scope

- Polar alignment for equatorial mode (S50 is alt-az; wedge use is out of scope for now); photometric/bias-dark-flat calibration (separate concern, see earlier draft superseded); permanent mount modeling.

## Further Notes

- Depends on: seestar-link (state/goto/frames + compat shim), catalog (bright-star suggestions), project-store (session records), observing (night flow + gating).
- Open questions for the user: how many alignment stars does the official app use (1 vs 3)? Should leveling be automatic (motors) or manual (user adjusts tripod) with live readout?
