# 03: Annotated export + solve UI

**What to build:** the GUI/export side of astrometry: an annotated PNG export of a stack (star labels + magnitudes from the crossmatch, FOV rectangle, scale bar) rendered over the tone-mapped image (same PIL->texture/export path as image-enhance), a "Solve" button in the image viewer for on-demand plate-solving, and a drift plot (RA/Dec offset vs time) in the session views. Demoable: open a stack -> Solve -> WCS saved; annotated PNG export contains labels/FOV/scale bar; session shows the drift plot.

**Blocked by:** astrometry 01, astrometry 02, gaia-crossmatch 03 (labels)

**Status:** implemented in 4954e2d+599f950 (merged)

- [x] Annotated PNG export (PIL/matplotlib over tone-mapped image): labels, mags, FOV rectangle, scale bar
- [x] "Solve" action in the image viewer (worker thread, clear failure toast)
- [x] Drift plot widget in the session/observe views (analysis plotting patterns)
- [x] Widget tests via the ui harness; full suite green

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `4954e2d+599f950`; test list: tests/test_astrometry_view.py, tests/test_astrometry_view_widgets.py, tests/test_astrometry_boundary.py. Re-verified during the issue-17 audit: all referenced commits exist on main and the listed targeted tests pass (`uv run pytest <files> -q -n auto`).
