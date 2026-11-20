# Spec: catalog

Status: completed 2026-11-20 — all 5 issues implemented and reviewed

## Problem Statement

Entering targets by name and finding good standard stars requires catalog lookups. The app should resolve object names to coordinates and suggest comparison stars near the target, without the user leaving the app.

## Solution

A catalog module that resolves object names to J2000 coordinates (SIMBAD / NASA resolver) and queries AAVSO-style standard star data around a position, with a local on-disk cache so repeated lookups are instant and offline-capable.

## User Stories

1. As an observer, I want to type "M42" or "V* bet Per" and get its J2000 coordinates, so that I can add it to the plan without looking anything up.
2. As an observer, I want a clear error when a name is ambiguous or unknown, so that I can refine the search.
3. As an observer, I want to see a small list of candidate matches with magnitudes, so that I pick the right object.
4. As an observer, I want the app to list known standard/comparison stars near my target, so that I can pick comps for photometry.
5. As an observer, I want catalog results cached locally, so that repeat lookups work offline and instantly.
6. As an observer, I want RA/Dec entered manually as an alternative to name search, so that I can point at anything.

## Implementation Decisions

- Name resolution: SIMBAD via astroquery (or direct SIMBAD HTTP), plus a NASA/SIMBAD fallback; return candidates with coordinates and magnitude.
- Standard stars: AAVSO sequence/chart data around the target position (VizieR/AAVSO API), filtered by angular distance and magnitude range.
- Cache: JSON files under the app config dir, keyed by query; TTL configurable.
- Coordinates always J2000; conversions handled in one place.
- Module boundary: pure functions/async queries returning plain data; no GTK imports.

## Testing Decisions

- Good test = mocked HTTP responses: name→coords mapping, ambiguity handling, comp-star filtering by distance/magnitude, cache hit/miss behavior.
- Prior art: astroquery's own test suite patterns for SIMBAD mocking.

## Out of Scope

- Full planetarium / sky chart rendering.
- Plate solving of scope images (the scope does this internally).
- Custom user star lists (may be added later).

## Further Notes

- The astronomy-oracle MCP available in this environment can be used during development to sanity-check object data, but the app itself must use public catalog APIs.
