# Spec: gaia-crossmatch

Status: completed 2026-11-20 (01 91704f1, 02 221cbed, 03 4f4b1fc)

## Problem Statement

The app can measure the target star and hand-picked comps, but knows nothing about the rest of the field. For cluster work and variability surveys the user wants every detected star in a stack cross-matched against Gaia DR3 (magnitudes, BP-RP color, parallax, proper motion), cached locally so repeat views are instant and offline-capable.

## Solution

A gaia-crossmatch module: auto-detect all sources on a session stack (DAOStarFinder-style detection), solve/assume the WCS, query Gaia DR3 around the field center via VizieR TAP, cross-match by sky position within a configurable radius, and store the cross-match table in the project. Results cached on disk keyed by field center + radius, like the catalog module.

## User Stories

1. As an observer, I want all stars in a stack auto-detected, so that I don't pick them by hand.
2. As an observer, I want each detected star cross-matched to Gaia DR3 (G, BP, RP, parallax, pmra, pmdec), so that I get colors and distances.
3. As an observer, I want the cross-match stored in the project per stack, so that it persists.
4. As an observer, I want repeat cross-matches served from the local cache, so that they work offline and instantly.
5. As an observer, I want to see the cross-match overlaid on the image (labels, magnitudes), so that I can inspect it.
6. As an observer, I want unmatched detected stars flagged, so that I know which are new/artifacts.
7. As an observer, I want a configurable match radius and magnitude limit, so that I control the table size.
8. As an observer, I want a clear error when the Gaia query fails and no cache exists, so that I know to go online.

## Implementation Decisions

- Detection: photutils `DAOStarFinder` on the stack (stacks only, per decision — subs are not auto-detected), with FWHM estimated from the image; detection threshold configurable.
- WCS: from the stack FITS header; if missing, fall back to astrometry (see astrometry spec) — cross-match requires WCS.
- Gaia query: VizieR TAP (astroquery.vizier or direct TAP HTTP) against Gaia DR3, cone search around field center, radius = field diagonal + margin; select G/BP/RP/parallax/proper motion.
- Cache: JSON under the app config dir keyed by rounded field center + radius, TTL configurable — same pattern as the catalog module.
- Storage: cross-match table (detected x/y, RA/Dec, Gaia source_id, mags, parallax, pm) saved into the project DB per stack.
- Module boundary: pure Python (detection + query + match → table); no GTK imports.

## Testing Decisions

- Good test = synthetic FITS stack with known WCS and stars at known positions + a mocked Gaia response: detection finds the injected stars, cross-match pairs them within tolerance, unmatched stars flagged; cache hit avoids a second query; query failure without cache raises a clear error.
- Prior art: catalog module's cache tests and photometry's synthetic-FITS tests.

## Out of Scope

- Per-sub-frame detection (stacks only), Gaia XP spectra, non-Gaia catalogs.

## Further Notes

- Enabler for hr-diagram, variable-scan. Depends on catalog (cache pattern, coordinate utilities), project-store (storage), image-enhance 01 (stack loading/viewer).

## Open Questions

- Exact VizieR TAP endpoint vs astroquery wrapper — decided in spec: use direct TAP HTTP with the catalog module's cache; astroquery only if TAP proves awkward.
- Match radius default (arcsec) and Gaia magnitude limit (e.g. G < 20) — propose 1.5 arcsec / G < 20, confirm.
- Behavior when stack has no WCS — block with a "plate-solve first" hint (ties into astrometry spec)?
