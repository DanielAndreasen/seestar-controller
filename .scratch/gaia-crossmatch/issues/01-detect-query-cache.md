# 01: Detection + Gaia TAP query + cache (pure core)

**What to build:** the pure (no-gi) core of gaia-crossmatch: auto-detect all sources on a session STACK (photutils DAOStarFinder, FWHM estimated from the image, configurable threshold), read the WCS from the stack FITS header (clear "plate-solve first" error when missing), query Gaia DR3 via direct VizieR TAP HTTP cone search around the field center (radius = field diagonal + margin; select G/BP/RP/parallax/pmra/pmdec, G < 20), cross-match detected stars by sky position within a configurable radius (default 1.5 arcsec), flag unmatched detections, and cache the Gaia response on disk keyed by rounded field center + radius with configurable TTL (same pattern as the catalog module cache). Demoable: synthetic FITS stack with known WCS + injected stars and a mocked TAP response -> detection finds the stars, cross-match pairs them within tolerance, unmatched flagged, second call served from cache, query failure without cache raises a clear error.

**Blocked by:** None (catalog cache pattern, project-store, image-enhance all done)

**Status:** ready-for-agent

- [ ] Pure `gaia/` package, no gi import (subprocess boundary test like photometry's)
- [ ] `detect_sources(stack_data, header, threshold=...)` -> detected x/y + RA/Dec via WCS (astropy.wcs); missing WCS -> clear error
- [ ] `query_gaia(center_ra, center_dec, radius_deg, g_limit=20.0)` direct TAP HTTP to VizieR Gaia DR3 + disk cache under the app config dir (keyed by rounded center+radius, TTL); cache hit avoids HTTP; failure without cache -> clear GaiaUnavailableError
- [ ] `crossmatch(detected, gaia_rows, radius_arcsec=1.5)` -> table rows (x, y, ra, dec, source_id, g, bp, rp, parallax, pmra, pmdec, matched flag)
- [ ] Tests: synthetic FITS + mocked TAP (no network); cache hit; failure path; match tolerance; no-gi boundary
- [ ] Full suite green

## Comments

(implementor fills in)
