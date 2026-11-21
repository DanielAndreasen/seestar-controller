# Spec: visibility-planner

Status: completed 2026-11-20 (01: 28cb0e6; 02: 4ce1e3a)

## Problem Statement

Planning a night by hand is guesswork. The user wants a visibility planner: altitude/azimuth curves for the queued targets over the night, twilight times, and moon position/phase, so that the queue can be ordered sensibly.

## Solution

A visibility-planner module: for each plan entry, compute altitude/azimuth vs. time for the observing site (astropy AltAz), mark astronomical twilight and moon altitude/phase, render a night timeline plot, and suggest queue ordering (e.g. by altitude windows).

## User Stories

1. As an observer, I want altitude curves for all queued targets over the night, so that I see when each is up.
2. As an observer, I want twilight start/end and moon altitude/phase marked, so that I plan around them.
3. As an observer, I want a suggested queue order (by altitude window / airmass), so that I optimize the night.
4. As an observer, I want the site coordinates and timezone configurable, so that the planner is correct for my location.
5. As an observer, I want the timeline exported as PNG, so that I can share it.

## Implementation Decisions

- astropy.coordinates: AltAz with the site location and obstime grid; sun/moon via astropy.coordinates.get_body.
- Suggestion: greedy by best altitude window, respecting existing order as tiebreak.
- Module boundary: pure Python (targets + site + night → curves + suggestion); view renders.

## Testing Decisions

- Good test = known site/date: twilight times match astropy reference within a minute; altitude curve peaks at transit; suggestion orders targets by transit time.
- Prior art: catalog coordinate-utility tests.

## Out of Scope

- Weather integration, slew-time optimization, multi-night planning.

## Further Notes

- Depends on observing (plan/queue model), catalog 02 (coordinates), ui-shell.

## Open Questions

- Site coordinates: manual entry vs. geolocation — propose manual entry saved in settings.
- Suggestion applies to the queue automatically or just shown as a hint? Propose hint with an "apply" button.
