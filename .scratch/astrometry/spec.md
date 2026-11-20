# Spec: astrometry

Status: ready-for-agent (3 issues; blocked by gaia-crossmatch)

## Problem Statement

Stacks and subs may lack a reliable WCS, and the user wants to know where the scope actually pointed, how accurate the pointing is, and how the field drifts over a night. The user also wants annotated exports (catalog labels, FOV rectangle) for sharing.

## Solution

An astrometry module: plate-solve frames (astrometry.net-style local solve or astropy WCS against Gaia via gaia-crossmatch), store the WCS per frame, compute pointing error vs. requested and drift over the session, and render annotated exports (labels, FOV, scale bar).

## User Stories

1. As an observer, I want any stack or sub plate-solved on demand, so that it gets a WCS.
2. As an observer, I want the solved WCS saved into the FITS header and the project, so that it persists.
3. As an observer, I want pointing error (requested vs. solved center) per session, so that I judge goto accuracy.
4. As an observer, I want a drift plot (RA/Dec offset vs. time) over the night, so that I see tracking quality.
5. As an observer, I want an annotated PNG export (star labels, magnitudes, FOV rectangle, scale bar), so that I can share the image.
6. As an observer, I want a clear failure message when a frame can't be solved, so that I know why.

## Implementation Decisions

- Solver: local astrometry.net (`solve-field`) if available, else astropy WCS + Gaia reference (gaia-crossmatch) with a simple pattern match; decide in implementation, spec allows either.
- Pointing error: session queue entry RA/Dec vs. solved center.
- Annotated export: PIL/matplotlib render over the tone-mapped image (same path as image-enhance export).
- Module boundary: pure Python (frame + reference → WCS + stats); view renders.

## Testing Decisions

- Good test = synthetic FITS with known WCS and stars: solver recovers the WCS within tolerance; pointing-error and drift math correct on injected offsets; annotated export contains labels.
- Prior art: image-enhance FITS loading tests, photometry WCS round-trip tests.

## Out of Scope

- Blind solve of arbitrary images outside the app's frames, full astrometry.net server setup.

## Further Notes

- Depends on gaia-crossmatch (Gaia reference), image-enhance 01 (FITS I/O), observing (queue entries for requested coords), project-store.

## Open Questions

- solve-field binary present on the desktop machine, or pure-Python Gaia pattern match? Propose: try solve-field, fall back to Gaia match.
- Annotated export format: PNG only, or also SVG with selectable labels?
