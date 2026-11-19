# 04: Photometry run + results view

**What to build:** running photometry over a session and seeing results — process all sub-frames of a session, show per-frame measurements (table + scatter), flag outliers, and save measurements into the project lightcurve. Demoable: temp project with synthetic subs → run → measurements in db and visible in the view.

**Blocked by:** 02, 03, project-store 05

**Status:** ready-for-agent

- [ ] Run photometry over all subs of a session (worker thread, progress shown)
- [ ] Results view: per-frame table + scatter plot, outliers flaggable
- [ ] Measurements appended to the project lightcurve (jd, mag, mag_err)
- [ ] Tests: end-to-end on temp project with synthetic FITS; outlier exclusion
