# 04: Photometry run + results view

**What to build:** running photometry over a session and seeing results — process all sub-frames of a session, show per-frame measurements (table + scatter), flag outliers, and save measurements into the project lightcurve. Demoable: temp project with synthetic subs → run → measurements in db and visible in the view.

**Blocked by:** 02, 03, project-store 05

**Status:** implemented in 545d3d4

- [x] Run photometry over all subs of a session (worker thread, progress shown)
- [x] Results view: per-frame table + scatter plot, outliers flaggable
- [x] Measurements appended to the project lightcurve (jd, mag, mag_err)
- [x] Tests: end-to-end on temp project with synthetic FITS; outlier exclusion
