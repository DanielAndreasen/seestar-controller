# Spec: offline-analysis

Status: completed 2026-11-20 (01: 1818a75+0ddf1e9; 02: f81651e; 03: 3d6a6a4)

## Problem Statement

After an observing night, the user wants to open an existing project in the GUI during the day — without the telescope connected and without starting any observation — and do data/image analysis on the collected frames: browse sessions and frames, run photometry, plate-solving, crossmatch, mosaic stitching, image enhancement, variable scans, H-R diagrams, period analysis, and session reports. Today the app is built around a live link session, so offline analysis is not a first-class mode.

## Solution

An offline project mode: the app can open a project directory directly (no Seestar connection, no session state required), shows all sessions/frames/stacks from the project store, and exposes every analysis feature (photometry, astrometry solve, gaia crossmatch overlay, mosaic, enhance, variable-scan, hr-diagram, period-analysis, session-report, moving-objects) against the stored data. Analysis actions that need the scope (goto, live preview, starting a night) are hidden or disabled in this mode.

## User Stories

1. As an observer, I want to open an existing project from disk without connecting to the telescope, so that I can analyze data during the day.
2. As an observer, I want to see all sessions, subs, stacks, and mosaics of the project in the GUI, so that I can pick what to analyze.
3. As an observer, I want to run aperture photometry on stored subs offline, so that I can refine measurements without the scope.
4. As an observer, I want to plate-solve stored frames offline, so that I can fix missing WCS after the fact.
5. As an observer, I want to run the Gaia crossmatch and see the overlay on stored stacks offline, so that I can inspect matched stars.
6. As an observer, I want to stitch a mosaic from stored stacks offline, so that I can build wide-field images later.
7. As an observer, I want to enhance stored images offline, so that I can produce presentation images during the day.
8. As an observer, I want to run the variable scan and H-R diagram on stored data offline, so that I can do the science without the scope.
9. As an observer, I want to run period analysis (fold + period finding) on a stored lightcurve offline, so that I can work on variable stars any time.
10. As an observer, I want to generate the session report offline, so that I can document a night later.
11. As an observer, I want scope-dependent actions (goto, live preview, start night) clearly disabled in offline mode, so that I don't trigger them by accident.
12. As an observer, I want the app to remember the last opened project, so that reopening it is one click.
13. As an observer, I want a clear indication in the UI that I am in offline mode, so that I know no telescope is connected.

## Implementation Decisions

- New "offline" entry point in the UI shell: open project directory -> project store opened read-write, link layer never started.
- A mode flag on the app/window (offline vs connected) that gates scope actions; analysis views work identically in both modes.
- All analysis modules already consume the project store, so offline mode is mostly UI wiring + gating, not new analysis code.
- Recent-projects list persisted in the app config dir.

## Testing Decisions

- Good test = open a synthetic project in offline mode with the link layer mocked to fail: all analysis views render from stored data; scope actions raise/are disabled; no network/scope calls happen.
- Prior art: projects-view tests, ui-shell widget tests.

## Out of Scope

- Editing/deleting raw frames (keep read-only for analysis; deletion is a separate concern).
- Cloud sync or multi-machine projects.

## Further Notes

- Depends on project-store, projects-view, ui-shell, and all analysis specs (photometry, astrometry, gaia-crossmatch, mosaic-stitch, image-enhance, variable-scan, hr-diagram, period-analysis, session-report, moving-objects).

## Open Questions

- Should offline mode be a separate app entry (`seestar-controller analyze <project>`) or a mode inside the existing GUI? Propose: mode inside the existing GUI with an "Open project (offline)" action.
