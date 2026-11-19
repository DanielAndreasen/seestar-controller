# 05: Measurements + lightcurve query

**What to build:** the lightcurve core — append per-frame measurements (frame FK, JD, mag, mag_err) to the project, query the full lightcurve across all sessions ordered by JD, and flag/exclude outlier points. Demoable: append measurements across two sessions, query returns merged ordered lightcurve, excluded points filtered.

**Blocked by:** 02, 03

**Status:** ready-for-agent

- [ ] `add_measurement(project, frame_id, jd, mag, mag_err)` appends a row
- [ ] `lightcurve(project, include_excluded=False)` returns all measurements ordered by JD across sessions
- [ ] `set_excluded(project, measurement_id, bool)` flags outliers
- [ ] Tests: multi-session merge ordering, exclusion filtering
