# Issue 07: catalog comp list is nearest-first only — cannot sort by magnitude (needed to pick comps of similar brightness)

**Status:** needs-triage

## Found while
Live session 2026-11-22 (project vw-cep): while picking comparison stars for VW Cep, the Catalog comps list could only be browsed nearest-first; finding comps with magnitude close to the target (the whole point of differential photometry) was guesswork.

## Cause
`src/seestar_controller/photometry/suggest.py` sorts suggestions nearest-first (by `distance_deg`) and `suggestions_panel.py` offers no sort control — just Search + Add. Each row does show `name — band mag — distance° away`.

## Expected
- A sort control in the Catalog comps panel: by distance (current), by magnitude, and ideally "closest in magnitude to the target pick" (most useful for comp selection).
- Rows already show magnitude; sorting by it (or filtering to a magnitude range around the target) makes comp picking practical.

## Scope
`src/seestar_controller/photometry/suggest.py` (sort key options), `src/seestar_controller/photometry/suggestions_panel.py` (sort dropdown), tests in `tests/test_photometry_suggest*.py` / widget tests.
