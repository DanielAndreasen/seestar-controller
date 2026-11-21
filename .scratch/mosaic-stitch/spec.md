# Spec: mosaic-stitch

Status: ready-for-agent (1 issue)

## Problem Statement

The scope's mosaic mode produces several stacks of adjacent tiles, but the user wants one wide-field image of the whole area, enhanced and exportable like a single stack.

## Solution

A mosaic-stitch module: take the tiles of a mosaic session, plate-solve/align them (WCS-based, with overlap blending), stitch into one wide-field FITS, and hand it to the existing enhance pipeline and export.

## User Stories

1. As an observer, I want all tiles of a mosaic session stitched into one FITS, so that I get a wide-field image.
2. As an observer, I want tiles aligned by their WCS (with overlap blending), so that seams are invisible.
3. As an observer, I want the stitched result saved in the project, so that it persists.
4. As an observer, I want to open the stitched image in the enhance view, so that I process it like any stack.
5. As an observer, I want a clear error when tiles don't overlap or lack WCS, so that I know why stitching failed.

## Implementation Decisions

- Alignment: WCS-based reprojection onto a common grid (astropy.wcs + reproject if installable, else simple shift by WCS delta); overlap = average or feather blend.
- Requires WCS per tile — depends on astrometry spec; fallback: scope-reported tile offsets.
- Stitched FITS stored in the project; enhance pipeline consumes it unchanged.
- Module boundary: pure Python (tiles + WCS → stitched FITS); no GTK.

## Testing Decisions

- Good test = synthetic tiles with known WCS offsets and overlapping stars: stitched image has stars at the right common-grid positions, seam blend smooth; missing-WCS tile raises a clear error.
- Prior art: image-enhance FITS tests.

## Out of Scope

- Drizzle-style resampling with cosmic-ray rejection, manual control-point alignment.

## Further Notes

- Depends on astrometry (WCS), image-enhance (pipeline), project-store, observing (mosaic sessions).

## Open Questions

- reproject as a new dependency, or simple WCS-delta shift — propose reproject if pip-clean, else shift+feather.
- Stitch per session automatically, or on demand per mosaic?
