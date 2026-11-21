# 01: Field-star variable scan core (pure module)

**What to build:** the pure (no-gi) core of variable-scan: take the gaia-crossmatch source list of a stack as picks, run the existing aperture-photometry pipeline (photometry/measure.py) over ALL sub-frames of ALL sessions of a project, build a lightcurve per field star (differential against the comp ensemble, falling back to the field median when no comps are picked), compute robust scatter (MAD) / median photometric error per star with a minimum-frame count and a configurable threshold, and rank variable candidates — skipping the target star and comps. Persist scan results in the project DB so they accumulate across nights. Demoable: synthetic project with one injected variable field star and constant others -> the variable ranks first, constants below threshold; results queryable per project.

**Blocked by:** gaia-crossmatch 01 (source list per stack — not yet ticketed), photometry 03/04 (done)

**Status:** implemented in 13f4a97 (merged)

- [ ] Pure `variable_scan/` package, no gi (subprocess boundary test); reuses photometry measure pipeline and store.frames/measurements
- [ ] `scan_project(project, threshold=..., min_frames=...)` -> per-star lightcurves + ranking (MAD / median error), target/comps excluded, field-median fallback when no comps
- [ ] Persistence: scan results table in project DB (schema bump + migration per project-store 07 pattern); query API `scan_results(project)`
- [ ] Threshold setting stored in project/settings
- [ ] Tests: synthetic subs with one injected variable -> ranked first; constants below threshold; persistence round-trip; no-gi boundary; no network
- [ ] Full suite green

## Comments

(implementor fills in)
