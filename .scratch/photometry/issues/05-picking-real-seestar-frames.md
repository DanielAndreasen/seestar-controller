# 05: Star picking on real Seestar frames (no WCS subs, RGB stacks)

**What to build:** make target/comp picking work on the real Seestar S50 frames stored in projects. Today clicking a star in the photometry view fails on real frames: sub-frames have NO celestial WCS (no CTYPE cards), so `pick_at` -> `pixel_to_sky` raises `PickingError: the FITS header has no WCS`; RGB stacks (NAXIS=3 with SIP distortion keywords) make `WCS(dict)` raise a 3-dimensional/SIP error. Decide and implement the behavior: (a) picking always works in PIXEL coordinates (picks stored as x/y; sky coords optional), (b) when a usable 2D celestial WCS exists, attach sky coords as today, (c) RGB stacks: build the WCS from the 2D celestial subset (drop the color axis / SIP) or use the first plane, (d) when no WCS exists, catalog-assist (comp suggestions) degrades gracefully with a clear "plate-solve first" hint (ties into the astrometry spec) instead of erroring the pick. Demoable: open a real stack/sub of the ngc7789 project, click to pick target + comps — no error, picks stored and measurable; with WCS present, sky coords attached.

**Blocked by:** None (photometry 01-04 done). Real debug data: FITS files under /home/daniel/SeestarProjects/ngc7789/subs/*/ and stacks/ (subs: NAXIS=2, no CTYPE; stacks: NAXIS=3 RGB + SIP). Tests must NOT read the user's home dir — copy a small real FITS into tests/fixtures/ or generate a synthetic equivalent.

**Status:** implemented in 8f00932 (merged)

- [x] `pick_at` works in pixel space on any frame; sky conversion only attempted when a 2D celestial WCS is present (RGB stack: WCS from celestial axes only, SIP dropped or handled); no-WCS frames pick fine, sky fields None
- [x] Catalog-assist/suggestion path degrades gracefully without WCS (clear hint, no exception)
- [x] Regression tests: synthetic Seestar-style sub (no CTYPE) and RGB stack (NAXIS=3 + SIP) -> pick_at succeeds, picks stored, measure works; WCS-present frame still gets sky coords; optional real-FITS fixture test
- [x] Full suite green; ty/ruff clean

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `8f00932`; test list: `tests/test_photometry_picking.py` and `tests/test_photometry_suggest.py` extended, new fixture `tests/fixtures/seestar_sub_no_wcs.fits`.
