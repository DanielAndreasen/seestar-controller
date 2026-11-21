# 02: Variable candidate view (ranked list + lightcurve + blink + CSV)

**What to build:** the GUI for variable-scan: a ranked candidate list (scatter ratio, threshold slider), clicking a candidate shows its lightcurve (reuse analysis lightcurve view) and a blink of the star's cutout across frames (animated GIF via PIL), plus CSV export of the candidate list. Demoable: run a scan on a project; candidates listed ranked; click one -> lightcurve + blink render; CSV exports.

**Blocked by:** variable-scan 01

**Status:** implemented in 3124427 (merged)

- [ ] Widget `VariableScanView`: ranked list (Gtk.ListBox) with scatter-ratio column, threshold control, per-candidate lightcurve (reuse LightcurveView) and blink (animated GIF of cutouts rendered via PIL, shown in the image viewer path)
- [ ] CSV export of candidates (reuse analysis/export.py)
- [ ] Wired into the app next to the photometry results view
- [ ] Widget tests via the ui harness; full suite green

## Comments

(implementor fills in)
