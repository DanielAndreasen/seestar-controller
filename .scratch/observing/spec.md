# Spec: observing

Status: completed 2026-11-21 — all 5 issues implemented and reviewed (05: 9533c092+d4beeb9)

## Problem Statement

The user wants to plan a night of observing and run it unattended from the office: a queue of targets, each pointed at and stacked, with the live preview visible and the session recorded to the project.

## Solution

An observing module with a plan/queue editor and an executor state machine driving seestar-link: for each queued target — resolve (catalog), goto, set exposure/gain/mosaic, start stacking, stream preview, log frames to project-store; then advance to the next target. Manual controls (pause, skip, stop, park) throughout.

## User Stories

1. As an observer, I want to add targets to tonight's plan by name (catalog search) or RA/Dec, so that I build the queue quickly.
2. As an observer, I want to set mosaic size per target, so that large objects fit.
3. As an observer, I want to reorder, remove, and duplicate queue entries, so that I shape the night.
4. As an observer, I want to set exposure/gain per target, so that each target is optimized.
5. As an observer, I want to start the night and have the scope execute the queue automatically, so that I don't babysit it.
6. As an observer, I want to see live preview of the current target while it stacks, so that I see progress.
7. As an observer, I want to see queue progress (current target, stack count, elapsed), so that I know how the night is going.
8. As an observer, I want to pause, skip, or stop the queue, so that I react to clouds or changes of plan.
9. As an observer, I want each night saved as a session in the current project, so that data accumulates.
10. As an observer, I want sub-frames downloaded into the session as they arrive, so that photometry can run on them.
11. As an observer, I want the scope parked at the end (manually or when the queue empties), so that it shuts down safely.
12. As an observer, I want a session log (events, errors, stack counts), so that I can review what happened.

## Implementation Decisions

- Queue model: ordered list of entries {name, ra, dec, mosaic, exposure, gain, duration-or-frame-count}.
- Executor: explicit state machine — idle → resolving → slewing → stacking → (next | done); events from seestar-link drive transitions; UI renders state.
- Preview: frames from the tele image socket rendered into the Observe view; latest stack shown alongside.
- Session: created in project-store at night start; frames appended as downloaded; closed at park/stop.
- Module boundary: executor logic independent of GTK (headless-testable); UI subscribes to state changes.

## Testing Decisions

- Good test = executor state machine against a mock seestar-link: queue ordering, auto-advance, pause/skip/stop, error mid-queue (scope error → skip or abort policy), session logging to a temp project.
- No real scope required.

## Out of Scope

- Weather/horizon/altitude constraint scheduling (user watches the preview).
- Meridian flip handling (alt-az mount).
- Mosaic stitching on the PC (the scope stacks the mosaic itself).

## Further Notes

- Depends on seestar-link, catalog, project-store, ui-shell.
