# Spec: image-enhance

Status: completed 2026-11-20 — all 4 issues implemented and reviewed

## Problem Statement

The scope's stacked FITS images look flat out of the box. The user wants the in-app enhancement the official app offers — stretch, denoise, sharpen, white balance, background subtraction — applied non-destructively.

## Solution

An enhancement module that loads a stack FITS from the project, applies a composable pipeline of non-destructive operations with adjustable parameters and a live before/after preview, and can save the enhanced result (and export PNG/JPEG). The original FITS is never modified.

## User Stories

1. As an observer, I want to open any stack from the current project, so that I can enhance it.
2. As an observer, I want a before/after preview, so that I see what each operation does.
3. As an observer, I want sliders for stretch (histogram/asinh), denoise strength, sharpen amount, white balance, background subtraction, so that I tune the result.
4. As an observer, I want undo/redo of parameter changes, so that I explore freely.
5. As an observer, I want the original FITS untouched, so that I can always go back.
6. As an observer, I want to save the enhanced image into the project, so that it's kept with the campaign.
7. As an observer, I want to export the enhanced image as PNG/JPEG, so that I can share it.
8. As an observer, I want my parameter presets remembered per image, so that reopening shows my last edit.

## Implementation Decisions

- Pipeline: ordered ops, each a pure function image+params → image; params stored per image in the project DB (`project.json`/db), applied on load.
- Ops: histogram/asinh stretch, denoise (wavelet or gaussian-based), unsharp mask sharpen, white balance (gray-world or per-channel gains), background subtraction (polynomial/median).
- Rendering: FITS → 16-bit numpy → tone-mapped 8-bit PNG via PIL for the preview; full-res export on demand.
- Libraries: astropy.io.fits, numpy, scipy, PIL. No GPU requirement.
- Module boundary: pipeline pure Python (no GTK); view binds sliders to params and re-renders.

## Testing Decisions

- Good test = synthetic FITS with known content: stretch preserves ordering, denoise reduces injected noise, sharpen increases edge contrast, white balance neutralizes a color cast, background subtraction flattens a gradient; original file hash unchanged after enhance+save.
- Golden-image regression: fixed input + params → fixed output hash.

## Out of Scope

- AI/ML denoise, PixInsight-level processing, drizzle stacking, mosaic stitching, annotation/drawing.

## Further Notes

- Depends on project-store (load/save) and ui-shell (view).
