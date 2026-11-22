# Issue 08: catalog comp suggestions are not limited to the open frame's FOV (default radius 1.5° >> S50 FOV)

**Status:** needs-triage

## Found while
Live session 2026-11-22 (project vw-cep, solved 10 s sub open in the Photometry picker): comps sorted by distance to the target were all outside the visible frame — the S50 FOV is well under 1.5° across, but `DEFAULT_COMP_SEARCH_RADIUS_DEG = 1.5` and nothing filters suggestions to the open image.

## Expected
- When the open frame has a WCS, suggestion results are filtered (or at least visually marked) to stars whose sky position falls inside the frame footprint (use `sky_to_pixel` / WCS pixel bounds from `photometry.picking`).
- The search radius default should reflect the actual FOV (e.g. ~0.5°) or auto-derive from the open frame's WCS extent when solved.
- Rows outside the frame should be hidden or greyed, not offered as Add-able comps.

## Scope
`src/seestar_controller/photometry/suggest.py` (FOV filter when an image/WCS is bound to the pick controller), `suggestions_panel.py` (grey/hide out-of-frame rows), `ui/state.py` default radius, tests in `tests/test_photometry_suggest*.py`.
