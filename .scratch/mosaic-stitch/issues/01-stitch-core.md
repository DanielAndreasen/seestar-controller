# 01: Mosaic stitch core — align tiles by WCS and stitch into one FITS (pure)

**What to build:** the pure (no-gi) core of mosaic-stitch: given the tile stacks of a mosaic session, align them on a common grid using their WCS (astropy.wcs reprojection; fallback: simple integer shift by WCS delta if reproject is unavailable), blend overlaps (average or feather), and write one stitched wide-field FITS into the project. Clear error when tiles lack WCS or don't overlap. Demoable: synthetic tiles with known WCS offsets + overlapping stars -> stitched FITS has stars at the right common-grid positions; missing-WCS tile raises a clear error.

**Blocked by:** None for the core (astrometry spec is separate; use tile WCS if present, else scope-reported tile offsets from the session log if available)

**Status:** implemented in 1c015c0 (merged 7585e3c)

- [x] Pure `mosaic/` package, no gi (subprocess boundary test); astropy lazy; reproject optional with fallback
- [x] `stitch_session(project, session_id) -> path` — collect tile stacks, read WCS per tile, common grid, reproject/shift, overlap blend (average/feather), write stitched FITS into the project (file-based, e.g. mosaics/<sid>.fits; no store schema bump)
- [x] Clear errors: tile without WCS -> MosaicError naming the tile; no overlap -> MosaicError
- [x] Tests: synthetic tiles with known WCS offsets + overlapping stars -> stars at right positions in stitched image, seam smooth; missing WCS -> clear error; no-overlap -> clear error; no-gi boundary; no network
- [x] Full suite green

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `1c015c0 (merged 7585e3c)`; test list: tests/test_mosaic_stitch.py. Re-verified during the issue-17 audit: all referenced commits exist on main and the listed targeted tests pass (`uv run pytest <files> -q -n auto`).
