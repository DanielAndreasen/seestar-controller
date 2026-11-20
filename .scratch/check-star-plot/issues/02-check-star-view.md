# 02: Check-star series in the photometry results view

**What to build:** the GUI: the photometry results view gains the check star's differential lightcurve as a second series next to the target's, plus a toggle for the target-minus-check curve; the check-star pick is selectable in the star picker and saved in the project. Demoable: run photometry on a session with a check star picked; results view shows target + check series and the target-minus-check toggle.

**Blocked by:** check-star-plot 01

**Status:** ready-for-agent

- [ ] Star picker: designate one picked star as check (role UI), persisted via store.stars
- [ ] Results view (photometry/results_view.py + analysis/lightcurve_view.py patterns): second series for the check star, toggle for target-minus-check
- [ ] Widget tests via the ui harness; full suite green

## Comments

(implementor fills in)
