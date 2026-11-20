# Spec: hr-diagram

Status: ready-for-agent (3 issues; blocked by gaia-crossmatch tickets, not yet written)

## Problem Statement

For star clusters the user wants a color-magnitude diagram (CMD) and a Hertzsprung-Russell (H-R) diagram of the field, with cluster membership and an isochrone overlay, to see the cluster's main sequence, turn-off, and distance/age at a glance.

## Solution

An hr-diagram module: take the gaia-crossmatch table of a stack, render (a) apparent CMD (G vs BP-RP) and (b) H-R diagram (absolute G from parallax vs BP-RP), with optional cluster membership filtering (parallax + proper motion), and overlay a PARSEC isochrone with adjustable age/metallicity/distance modulus.

## User Stories

1. As an observer, I want a CMD (apparent G vs BP-RP) of all cross-matched stars in a stack, so that I see the cluster sequence.
2. As an observer, I want an H-R diagram (absolute G vs BP-RP) of the same stars, so that I see the physical diagram.
3. As an observer, I want cluster membership filtering by parallax and proper motion, so that field stars drop out.
4. As an observer, I want a PARSEC isochrone overlay with adjustable age, metallicity, and distance modulus, so that I estimate cluster parameters.
5. As an observer, I want to click a star in the diagram to highlight it on the image (and vice versa), so that I inspect individual stars.
6. As an observer, I want to zoom/pan and toggle error bars, so that I inspect details.
7. As an observer, I want the plot exported as PNG and the underlying table as CSV, so that I can use it elsewhere.
8. As an observer, I want the chosen isochrone parameters saved in the project, so that they persist.

## Implementation Decisions

- Data: gaia-crossmatch table per stack; absolute G = G + 5 - 5*log10(1000/parallax_mas) with parallax SNR cut (e.g. parallax/parallax_error > 5).
- Isochrones: PARSEC via a bundled/queried table (decide: fetch from a fixed URL and cache, or bundle a small grid) — open question below.
- Rendering: matplotlib (Agg) to PNG for embedding in GTK via the same PIL->texture path as image-enhance; interactive pan/zoom via matplotlib widget or a simple re-render on zoom.
- Module boundary: pure Python (table + isochrone → plot data/PNG); view binds to it.

## Testing Decisions

- Good test = synthetic cross-match table with a known cluster (injected main sequence + field stars): membership filter keeps cluster stars, absolute-G math correct within tolerance, isochrone overlay renders, CSV export round-trips.
- Prior art: period-analysis plot rendering and CSV export tests.

## Out of Scope

- Isochrone fitting/optimization (user adjusts parameters manually), spectroscopy.

## Further Notes

- Depends on gaia-crossmatch, project-store, ui-shell. Both CMD and H-R views per user decision.

## Open Questions

- Isochrone source: cached PARSEC web fetch vs bundled grid — propose cached fetch with bundled fallback.
- Membership filter defaults (parallax window, pm radius) — propose sigma-clipping around the median parallax/pm.
- One diagram per stack, or aggregate across sessions of a project?
