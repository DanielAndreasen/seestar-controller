# 02: Coordinate utilities

**What to build:** the shared coordinate math — parse and validate RA/Dec strings (sexagesimal and decimal degrees), normalize everything to J2000, and compute angular distance between sky positions. Used by standard-star filtering and star picking.

**Blocked by:** seestar-link 01 (uv project scaffold)

**Status:** implemented in 4544182

- [x] RA/Dec parsing accepts sexagesimal and decimal forms, rejects invalid input
- [x] All coordinates normalized to J2000 in one place
- [x] Angular distance helper correct on sphere (incl. wraparound at RA 0)
- [x] Unit tests with known coordinate pairs
