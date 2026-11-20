# Spec: variable-scan

Status: ready-for-agent (2 issues; blocked by gaia-crossmatch tickets, not yet written)

## Problem Statement

The user monitors one target star, but other stars in the field may be variable too. The user wants an automatic scan of all cross-matched stars' lightcurves across the project's sessions, flagging candidates whose scatter exceeds their photometric errors.

## Solution

A variable-scan module: run aperture photometry on all detected stars (from gaia-crossmatch) over all subs of all sessions, build a lightcurve per star, compute scatter vs. median error, and rank candidates; show a blink view (frames side by side / animated) for visual confirmation.

## User Stories

1. As an observer, I want photometry run automatically on all detected field stars, so that I get a lightcurve per star.
2. As an observer, I want a ranked list of variable candidates (scatter / median error), so that I see the most likely variables first.
3. As an observer, I want to click a candidate to see its lightcurve and a blink of the frames, so that I confirm it visually.
4. As an observer, I want the scan to skip the target star and comps (already handled), so that the list is clean.
5. As an observer, I want scan results saved in the project, so that they accumulate across nights.
6. As an observer, I want a threshold setting for the scatter ratio, so that I control sensitivity.
7. As an observer, I want to export the candidate list as CSV, so that I can follow up elsewhere.

## Implementation Decisions

- Reuses photometry's aperture pipeline with the gaia-crossmatch source list as picks; per-star differential mag against the comp ensemble (or against the field median if no comps).
- Detection on stacks only (per decision); photometry still on subs.
- Ranking: robust scatter (MAD) / median error, with minimum number of frames.
- Blink: render the star's cutout across frames as an animated GIF/PNG sequence via PIL.
- Module boundary: pure Python (frames + source list → per-star lightcurves + ranking); view renders.

## Testing Decisions

- Good test = synthetic subs with one injected variable field star and constant others: the variable is ranked first, constant stars below threshold; blink frames generated.
- Prior art: photometry synthetic-FITS tests.

## Out of Scope

- Period finding per candidate (that's period-analysis, applied to the candidate), classification (RR Lyrae vs eclipsing etc.).

## Further Notes

- Depends on gaia-crossmatch, photometry, project-store.

## Open Questions

- Scan scope: whole project (all sessions) or selected sessions?
- Differential against comp ensemble or field median when no comps picked — propose field median fallback.
- Blink format: animated GIF vs side-by-side PNG grid?
