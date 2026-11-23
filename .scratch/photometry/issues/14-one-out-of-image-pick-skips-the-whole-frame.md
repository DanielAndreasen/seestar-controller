# Issue 14: one out-of-image pick skips the ENTIRE frame — 146/512 frames (28.5%) lost when one comp drifted off the edge

**Status:** needs-triage

## Found while
Headless lightcurve run 2026-11-22 (vw-cep): skip_reasons = {"outside-image": 146}. Every skip was a WCS-reprojected COMP pick landing outside the 1080×1920 frame (Comp C off the right edge in 138 frames, Comp D bottom in 9, Comp B top in 3) — the target and other comps were perfectly measurable, but one bad pick fails the whole frame in `measure_frame_data`.

## Expected
- Per-PICK skip semantics: a pick that lands outside the image (or fails refinement) is dropped for that frame with a recorded reason; the frame is measured if the target + at least one comp remain. The frame is skipped only if the TARGET pick is unusable (or no comp left).
- RunReport skip reasons should distinguish frame-level vs pick-level drops (e.g. `pick-outside-image×N` aggregated per pick label).
- Tests: frame where one comp is off-edge → still measured, report notes the dropped pick; frame where target off-edge → frame skipped.

## Scope
`src/seestar_controller/photometry/measure.py` (`measure_frame_data` per-pick handling), `run.py` (report aggregation), tests.
