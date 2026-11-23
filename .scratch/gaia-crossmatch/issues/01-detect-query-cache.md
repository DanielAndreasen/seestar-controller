# 01: Detection + Gaia TAP query + cache (pure core)

**What to build:** the pure (no-gi) core of gaia-crossmatch: auto-detect all sources on a session STACK (photutils DAOStarFinder, FWHM estimated from the image, configurable threshold), read the WCS from the stack FITS header (clear "plate-solve first" error when missing), query Gaia DR3 via direct VizieR TAP HTTP cone search around the field center (radius = field diagonal + margin; select G/BP/RP/parallax/pmra/pmdec, G < 20), cross-match detected stars by sky position within a configurable radius (default 1.5 arcsec), flag unmatched detections, and cache the Gaia response on disk keyed by rounded field center + radius with configurable TTL (same pattern as the catalog module cache). Demoable: synthetic FITS stack with known WCS + injected stars and a mocked TAP response -> detection finds the stars, cross-match pairs them within tolerance, unmatched flagged, second call served from cache, query failure without cache raises a clear error.

**Blocked by:** None (catalog cache pattern, project-store, image-enhance all done)

**Status:** implemented in 91704f1 (merged 250de0e)

- [x] Pure `gaia/` package, no gi import (subprocess boundary test like photometry's)
- [x] `detect_sources(stack_data, header, threshold=...)` -> detected x/y + RA/Dec via WCS (astropy.wcs); missing WCS -> clear error
- [x] `query_gaia(center_ra, center_dec, radius_deg, g_limit=20.0)` direct TAP HTTP to VizieR Gaia DR3 + disk cache under the app config dir (keyed by rounded center+radius, TTL); cache hit avoids HTTP; failure without cache -> clear GaiaUnavailableError
- [x] `crossmatch(detected, gaia_rows, radius_arcsec=1.5)` -> table rows (x, y, ra, dec, source_id, g, bp, rp, parallax, pmra, pmdec, matched flag)
- [x] Tests: synthetic FITS + mocked TAP (no network); cache hit; failure path; match tolerance; no-gi boundary
- [x] Full suite green

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `91704f1 (merged 250de0e)`; test list: tests/test_gaia_detect.py, tests/test_gaia_query.py, tests/test_gaia_crossmatch.py, tests/test_gaia_pipeline.py, tests/test_gaia_boundary.py. Re-verified during the issue-17 audit: all referenced commits exist on main and the listed targeted tests pass (`uv run pytest <files> -q -n auto`).
