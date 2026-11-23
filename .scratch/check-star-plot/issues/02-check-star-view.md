# 02: Check-star series in the photometry results view

**What to build:** the GUI: the photometry results view gains the check star's differential lightcurve as a second series next to the target's, plus a toggle for the target-minus-check curve; the check-star pick is selectable in the star picker and saved in the project. Demoable: run photometry on a session with a check star picked; results view shows target + check series and the target-minus-check toggle.

**Blocked by:** check-star-plot 01

**Status:** implemented in 16a5334 (merged)

- [x] Star picker: designate one picked star as check (role UI), persisted via store.stars
- [x] Results view (photometry/results_view.py + analysis/lightcurve_view.py patterns): second series for the check star, toggle for target-minus-check
- [x] Widget tests via the ui harness; full suite green

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `16a5334`; test list: tests/test_photometry_widgets.py. Re-verified during the issue-17 audit: all referenced commits exist on main and the listed targeted tests pass (`uv run pytest <files> -q -n auto`).
