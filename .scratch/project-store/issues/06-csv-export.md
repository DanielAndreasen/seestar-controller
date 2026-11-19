# 06: CSV export

**What to build:** exporting the lightcurve as `lightcurve.csv` in the project directory with columns `jd, utc, mag, mag_err, session, target`, re-parseable by standard tools. Demoable: export a project's lightcurve, re-parse the CSV, values match the db.

**Blocked by:** 05

**Status:** ready-for-agent

- [ ] `export_csv(project)` writes `lightcurve.csv` with header `jd,utc,mag,mag_err,session,target`
- [ ] Export reflects exclusion flags (excluded rows omitted or marked — pick one, document it)
- [ ] Tests: export → re-parse round-trip matches db values
