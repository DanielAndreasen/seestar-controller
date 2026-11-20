# 04: Standard-star lookup near target

**What to build:** given target coordinates, query AAVSO/VizieR for known standard (comparison) stars within a radius, filtered and sorted by magnitude proximity to the target — the catalog assist for photometry comp picking. Demoable: mocked catalog returns stars, result filtered by distance and magnitude.

**Blocked by:** 01, 02

**Status:** implemented in 1b12d96

- [x] Given RA/Dec + radius, returns standard stars with coords and magnitudes
- [x] Results filtered by angular distance and sorted by magnitude closeness to the target
- [x] Empty result (no standards nearby) is a normal, handled outcome
- [x] Tests with mocked VizieR/AAVSO responses cover filtering and sorting
