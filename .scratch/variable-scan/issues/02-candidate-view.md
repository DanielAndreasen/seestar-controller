# 02: Variable candidate view (ranked list + lightcurve + blink + CSV)

**What to build:** the GUI for variable-scan: a ranked candidate list (scatter ratio, threshold slider), clicking a candidate shows its lightcurve (reuse analysis lightcurve view) and a blink of the star's cutout across frames (animated GIF via PIL), plus CSV export of the candidate list. Demoable: run a scan on a project; candidates listed ranked; click one -> lightcurve + blink render; CSV exports.

**Blocked by:** variable-scan 01

**Status:** implemented in 3124427 (merged)

- [x] Widget `VariableScanView`: ranked list (Gtk.ListBox) with scatter-ratio column, threshold control, per-candidate lightcurve (reuse LightcurveView) and blink (animated GIF of cutouts rendered via PIL, shown in the image viewer path)
- [x] CSV export of candidates (reuse analysis/export.py)
- [x] Wired into the app next to the photometry results view
- [x] Widget tests via the ui harness; full suite green

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `3124427`; test list: `tests/test_variable_scan_view.py` and `tests/test_variable_scan_view_widgets.py` (added there).
