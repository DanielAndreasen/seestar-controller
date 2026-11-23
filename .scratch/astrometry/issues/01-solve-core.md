# 01: Plate-solve core (pure) — WCS into FITS header + project

**What to build:** the pure (no-gi) solver: plate-solve a stack or sub on demand — try the local astrometry.net `solve-field` binary if present on the machine, else fall back to a pure-Python Gaia pattern match using the gaia-crossmatch table (detected stars vs Gaia sources); write the solved WCS into the FITS header and persist it in the project; clear failure message when a frame can't be solved. Demoable: synthetic FITS with known WCS + stars -> solver recovers the WCS within tolerance and writes it back; unsolvable frame -> clear error.

**Blocked by:** gaia-crossmatch 01 + 02 (Gaia reference for the fallback solver)

**Status:** implemented in 8f671c1 (merged 4da4d51)

- [x] Pure `astrometry/` package, no gi (subprocess boundary test)
- [x] `solve_frame(fits_path or data+header)` -> WCS; solve-field subprocess if binary found (shutil.which), else Gaia pattern match via gaia-crossmatch; WCS written into the FITS header (astropy) + stored in project DB per frame
- [x] `SolveError` with a clear message on failure
- [x] Tests: synthetic FITS with known WCS -> recovered within tolerance, header round-trip; failure path; no network (mock Gaia table)
- [x] Full suite green

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `8f671c1 (merged 4da4d51)`; test list: tests/test_astrometry_solve.py, tests/test_astrometry_store.py, tests/test_astrometry_boundary.py. Re-verified during the issue-17 audit: all referenced commits exist on main and the listed targeted tests pass (`uv run pytest <files> -q -n auto`).
