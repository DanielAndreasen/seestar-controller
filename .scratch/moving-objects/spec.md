# Spec: moving-objects

Status: completed 2026-11-20 (01 e754922, 02 b243dbd)

## Problem Statement

Asteroids and satellites streak across the field during a session and are invisible in the final stack. The user wants them detected and reported — position over time, and optionally identified against the MPC catalog.

## Solution

A moving-objects module: detect sources per sub-frame, link detections across frames into tracks, flag linear movers (constant angular velocity), report track coordinates/times, optionally query the MPC for a known-object match, and render an annotated blink/overlay.

## User Stories

1. As an observer, I want sources detected in every sub-frame of a session, so that movers can be tracked.
2. As an observer, I want detections linked into tracks across frames, so that I see motion.
3. As an observer, I want linear movers flagged with angular velocity and direction, so that I identify asteroids/satellites.
4. As an observer, I want a report of movers (time, RA/Dec track, speed), so that I can follow up.
5. As an observer, I want an optional MPC lookup for each mover, so that I know if it's a known asteroid.
6. As an observer, I want an annotated overlay/blink on the frames, so that I see the mover visually.
7. As an observer, I want movers saved in the project session, so that they persist.

## Implementation Decisions

- Detection per sub (photutils DAOStarFinder, low threshold); linking by nearest-neighbor in sky coords with a max motion per frame; track = >= N frames with consistent velocity (RANSAC-lite or linear fit residual cut).
- MPC query via the catalog module's HTTP+cache pattern.
- Module boundary: pure Python (subs → tracks → report); view renders overlay.

## Testing Decisions

- Good test = synthetic subs with one injected linear mover + static stars: mover track recovered with correct speed/direction, static stars not flagged; MPC lookup mocked.
- Prior art: photometry synthetic-FITS tests, catalog cache tests.

## Out of Scope

- Orbit determination, satellite catalogs beyond MPC, streak removal from stacks.

## Further Notes

- Depends on project-store (subs), catalog (HTTP/cache), astrometry (WCS per sub — subs may lack WCS; open question).

## Open Questions

- Subs often lack WCS — plate-solve subs first (astrometry spec) or use the stack WCS for all subs? Propose stack WCS + per-sub offset from the scope's reported pointing.
- Minimum track length (frames) to flag a mover — propose >= 5.
