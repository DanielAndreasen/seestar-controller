# Spec: check-star-plot

Status: ready-for-agent (2 issues)

## Problem Statement

Differential photometry is only trustworthy if the comps themselves are constant. The user wants a check-star plot — a chosen check star measured against the comp ensemble, exactly like the target — so that systematic errors (passing clouds, drift) are visible.

## Solution

Extend the photometry view with a check-star role: pick a check star, compute its differential magnitude against the comp ensemble per frame, and plot it alongside the target's lightcurve (and optionally target-minus-check).

## User Stories

1. As an observer, I want to designate one picked star as the check star, so that it is measured like the target.
2. As an observer, I want the check star's differential lightcurve plotted next to the target's, so that I compare them.
3. As an observer, I want a target-minus-check curve, so that systematics cancel.
4. As an observer, I want the check star excluded from the comp ensemble, so that it stays independent.
5. As an observer, I want the check-star choice saved in the project, so that it persists.

## Implementation Decisions

- StarPickSpec role gains `check`; comp ensemble = mean of comps only.
- Check differential mag computed with the same error propagation as the target.
- Plot: photometry results view gains a second series; pure-Python data prep, GTK rendering.

## Testing Decisions

- Good test = synthetic frames where a cloud dims everything: target and check move together, target-minus-check stays flat within errors; check excluded from ensemble math.
- Prior art: photometry ensemble tests.

## Out of Scope

- Multiple check stars, automated check-star quality verdicts.

## Further Notes

- Small feature; depends on photometry (01-04 done), project-store 04.

## Open Questions

- One check star or several — propose one, extend later.
- Plot in the existing results view or a separate tab?
