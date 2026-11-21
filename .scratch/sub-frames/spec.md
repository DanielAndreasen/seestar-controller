# Spec: sub-frames workflow (view / solve / analyze raw subs)

Status: ready-for-agent (3 issues)

## Problem Statement

Real Seestar projects often contain only raw subs (kind='sub') — e.g. an interrupted session with two FITS files and an empty stacks/ folder. Today the desktop app's image viewer and the whole analysis chain (plate-solve, Gaia crossmatch, H-R diagram, photometry picking) only accept kind='stack' frames, so subs are invisible and unusable in the GUI: the Enhance viewer list is empty, "Stitch mosaic" refuses (it stitches tile stacks), and no H-R diagram can be built from them. Subs also carry no WCS, so they must be plate-solved before crossmatch/photometry.

## Solution

Make stored subs first-class frames in the offline workflow:

1. The image viewer lists and opens ANY stored frame (subs included), not just stacks.
2. The analysis chain (Solve, Gaia crossmatch overlay, H-R diagram, photometry picking) accepts solved subs: plate-solve a sub (existing astrometry solver), persist the WCS, then crossmatch and build the H-R diagram from that frame.
3. Optionally stack subs into a kind='stack' product offline, so the existing stack-based views work unchanged.

## User Stories

1. As a user, I want to open a raw sub in the image viewer, so that I can see what the scope actually captured even when no stack exists.
2. As a user, I want to plate-solve a sub from the viewer, so that it gets a WCS like a stack.
3. As a user, I want to run the Gaia crossmatch on a solved sub, so that I get star positions + G mags + parallaxes.
4. As a user, I want to build an H-R diagram from a solved+crossmatched sub, so that I can do science on interrupted sessions.
5. As a user, I want to run photometry picking on a solved sub, so that I can measure magnitudes frame by frame.
6. As a user, I want to stack my subs into a stack offline, so that all stack-based views (enhance, mosaic, report) work on them.
7. As a user, I want the Stored-data browser frame rows to open the frame in the viewer on click, so that browsing and viewing connect.

## Implementation Decisions

- Viewer: `list_project_stacks` usage in `enhance/viewer_controller.py` generalized to list all frames (or add `list_project_frames`); `open_stack` already loads by frame id — verify it works for subs (RGB→luminance path already exists in astrometry/export.py; reuse in enhance).
- Solve: `SolveController.solve_open_stack` → accept any open frame; persist WCS via store.wcs for sub frame ids too (check store.wcs accepts any frame id).
- Crossmatch/H-R/photometry: consume crossmatch rows keyed by frame id regardless of kind; H-R view's stack selector lists solved frames (subs with WCS + crossmatch).
- Stacking subs: pure function (astropy/numpy median or mean combine of aligned subs — subs from one session are already aligned by the scope) writing kind='stack' via store; UI button on session rows next to "Stitch mosaic" ("Stack subs").
- Pure/widget split per AGENTS.md; no gi in pure modules; no network in tests (solver + Gaia monkeypatched).

## Testing Decisions

- Pure tests: frame listing includes subs; open sub renders; solve persists WCS for sub id; crossmatch/H-R accept sub frame ids; stack-subs produces a valid stack FITS readable by list_project_stacks. No gi, no network.
- Widget tests via ui harness (gi-gated, `.emit("clicked")`, no destroy()): viewer lists subs, click opens, Solve button on sub, browser row click opens frame.
- Full suite green modulo known pre-existing headless failures.

## Out of Scope

- Drizzle/registration of misaligned subs (scope subs are already aligned; mosaic stitching of tiles is the existing mosaic-stitch spec).
- Changing store schema unless strictly needed (prefer reusing frame ids across kinds).

## Further Notes

- Depends on: offline-analysis (offline mode), astrometry (solve + WCS store), gaia-crossmatch, hr-diagram, photometry, mosaic-stitch — all merged on main.
- Known limitation from photometry 05: Seestar subs have no WCS and stacks are RGB — solving subs addresses the first; RGB handling already exists.
