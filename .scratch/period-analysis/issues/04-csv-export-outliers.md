# 04: CSV export + outlier marking

**What to build:** export and data hygiene — export the lightcurve (and folded data with phase column) as CSV via project-store, and mark/exclude outlier points from the UI so they don't distort the periodogram. Demoable: export CSV re-parses; excluded point disappears from periodogram input.

**Blocked by:** 01, 03, project-store 06

**Status:** ready-for-agent

- [ ] Export lightcurve CSV (jd, utc, mag, mag_err, session, target) and folded CSV (+ phase)
- [ ] Outlier points markable/excludable from the plot UI
- [ ] Excluded points omitted from periodogram and folded plot
- [ ] Tests: CSV round-trip; exclusion affects analysis input
