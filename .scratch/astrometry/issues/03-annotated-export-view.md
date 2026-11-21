# 03: Annotated export + solve UI

**What to build:** the GUI/export side of astrometry: an annotated PNG export of a stack (star labels + magnitudes from the crossmatch, FOV rectangle, scale bar) rendered over the tone-mapped image (same PIL->texture/export path as image-enhance), a "Solve" button in the image viewer for on-demand plate-solving, and a drift plot (RA/Dec offset vs time) in the session views. Demoable: open a stack -> Solve -> WCS saved; annotated PNG export contains labels/FOV/scale bar; session shows the drift plot.

**Blocked by:** astrometry 01, astrometry 02, gaia-crossmatch 03 (labels)

**Status:** implemented in 4954e2d+599f950 (merged)

- [ ] Annotated PNG export (PIL/matplotlib over tone-mapped image): labels, mags, FOV rectangle, scale bar
- [ ] "Solve" action in the image viewer (worker thread, clear failure toast)
- [ ] Drift plot widget in the session/observe views (analysis plotting patterns)
- [ ] Widget tests via the ui harness; full suite green

## Comments

(implementor fills in)
