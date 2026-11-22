# Issue 06: the Photometry view is too crowded — picking + results + mover overlay + variable-scan in one vertical paned

**Status:** needs-triage

## Found while
Live session 2026-11-22 (project vw-cep): guiding a user through the first lightcurve, the Photometry view was "extremely difficult to do anything on — so much going on on so little space".

## Cause
`src/seestar_controller/ui/views.py::PhotometryView` stacks everything into one vertical `Gtk.Paned`: the star picker (stack list + role/aperture + picks + catalog comps + preview) on top, the results panel (Run/Refresh + measurements + plots) below, PLUS the moving-objects overlay widget and the variable-scan widget — four major workflows in one view.

## Expected
- Split into tabs or a sidebar within the view: "Pick stars", "Run photometry", "Variables", "Movers" — one workflow visible at a time, each with room to breathe.
- Or at minimum: collapsible sections / an `Adw.ViewSwitcher` so the picking preview gets the full width while picking.
- Related gap found in the same session: the picker has **no way to enter a sky coordinate / target name** — `PickController.add_at_sky` exists in the pure layer but no widget exposes it, and there is no hover sky-coord readout, so the user must recognize the target star visually. Add a "go to RA/Dec" entry (or name resolve) that places the target pick via `add_at_sky` on the solved frame.

## Scope
`src/seestar_controller/ui/views.py` (PhotometryView), widgets in `photometry/star_picker.py`, `photometry/results_view.py`, `moving_objects/overlay_view.py`, `variable_scan/view.py`, tests in `tests/test_photometry_widgets.py` / `tests/test_ui_*.py`.
