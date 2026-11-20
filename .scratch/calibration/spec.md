# Spec: calibration

Status: needs-info

## Problem Statement

Instrument calibration (leveling the scope, star alignment) is physical work done at the scope — the user adjusts the tripod while standing next to it, using the official Seestar phone app. The desktop app cannot do that part. What the desktop app should do is know whether the scope is calibrated: read the level/alignment state from the device, show it, record it per session, and warn before starting a night if the mount was not calibrated.

## Solution

A calibration-status flow: the desktop app polls/reads the scope's calibration state (level OK, alignment done — via the fw 9.75 app-state tree through the link compat shim), displays it in the Observe view, records it in the session log when a night starts, and warns (not blocks) on "Start night" if calibration is missing or stale. Optionally, the desktop can verify pointing independently (slew to a bright star, capture, plate-solve locally) as a cross-check.

## User Stories

1. As a user, I level and align the scope with the official phone app while standing at the scope, so that the physical calibration is done where I can adjust it.
2. As a user, I want the desktop app to show the scope's current level/alignment state (read from the device), so that I can confirm from my office that calibration was done.
3. As a user, I want "Start night" to warn if the scope reports not-level or not-aligned, so that I don't waste a night on a misaligned mount — but still allow me to proceed deliberately.
4. As a user, I want the calibration state at night start recorded in the session log, so that later analysis knows the mount was calibrated.
5. As a user, I optionally want a desktop-side pointing cross-check (goto a bright star, plate-solve a frame, compare), so that I can verify alignment quality without going outside.

## Implementation Decisions

- Pure `calibration/` package (no gi): parse level/alignment fields from the device app-state tree (fw 9.75 `iscope_get_app_state` via link/compat.py; probe the live scope read-only to discover the exact fields — never send mutating commands with valid params), calibration-state model (level_ok, aligned, timestamp, source), session recording via store/sessions (schema bump + migration per project-store 07 pattern).
- Link: read-only use of Device.state(); any new wire method probed live read-only/bad-param only, added to link/compat.py with legacy fallback + mock support (firmware knob).
- Optional plate-solve cross-check: local solver (astrometry.net solve-field or WCS fit), offline; only if cheap — otherwise out of scope for v1.
- GUI: calibration status card in the Observe view (level/alignment indicators, last-calibrated time, refresh), warning toast/inline on Start night when not calibrated; state recorded into the session at night start.
- No driving of leveling motors from the desktop app.

## Testing Decisions

- Pure tests: app-state parsing for level/alignment (sample trees for legacy + 9.75), state model transitions, session recording — no gi, no network.
- Mock-scope tests: mock serves app-state with calibration fields (firmware knob), Observe controller warning logic.
- Widget tests via ui harness; full suite stays green.

## Out of Scope

- Driving the leveling motors / performing alignment from the desktop app (done with the phone app at the scope); bias/dark/flat calibration (different concern); polar alignment in equatorial mode.

## Further Notes

- Depends on: seestar-link (state + compat shim), project-store (session records), observing (night flow + warning).
- Open question for the user: does the fw 9.75 app-state tree actually expose level/alignment fields, or does the phone app compute them locally? (To be answered by read-only live probing during implementation.)
