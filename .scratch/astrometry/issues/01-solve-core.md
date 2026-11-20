# 01: Plate-solve core (pure) — WCS into FITS header + project

**What to build:** the pure (no-gi) solver: plate-solve a stack or sub on demand — try the local astrometry.net `solve-field` binary if present on the machine, else fall back to a pure-Python Gaia pattern match using the gaia-crossmatch table (detected stars vs Gaia sources); write the solved WCS into the FITS header and persist it in the project; clear failure message when a frame can't be solved. Demoable: synthetic FITS with known WCS + stars -> solver recovers the WCS within tolerance and writes it back; unsolvable frame -> clear error.

**Blocked by:** gaia-crossmatch 01 + 02 (Gaia reference for the fallback solver)

**Status:** ready-for-agent

- [ ] Pure `astrometry/` package, no gi (subprocess boundary test)
- [ ] `solve_frame(fits_path or data+header)` -> WCS; solve-field subprocess if binary found (shutil.which), else Gaia pattern match via gaia-crossmatch; WCS written into the FITS header (astropy) + stored in project DB per frame
- [ ] `SolveError` with a clear message on failure
- [ ] Tests: synthetic FITS with known WCS -> recovered within tolerance, header round-trip; failure path; no network (mock Gaia table)
- [ ] Full suite green

## Comments

(implementor fills in)
