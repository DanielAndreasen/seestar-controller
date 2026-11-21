# 01: Field-star variable scan core (pure module)

**What to build:** the pure (no-gi) core of variable-scan: take the gaia-crossmatch source list of a stack as picks, run the existing aperture-photometry pipeline (photometry/measure.py) over ALL sub-frames of ALL sessions of a project, build a lightcurve per field star (differential against the comp ensemble, falling back to the field median when no comps are picked), compute robust scatter (MAD) / median photometric error per star with a minimum-frame count and a configurable threshold, and rank variable candidates — skipping the target star and comps. Persist scan results in the project DB so they accumulate across nights. Demoable: synthetic project with one injected variable field star and constant others -> the variable ranks first, constants below threshold; results queryable per project.

**Blocked by:** gaia-crossmatch 01 (source list per stack — not yet ticketed), photometry 03/04 (done)

**Status:** implemented in 13f4a97 (merged)

- [x] Pure `variable_scan/` package, no gi (subprocess boundary test); reuses photometry measure pipeline and store.frames/measurements
- [x] `scan_project(project, threshold=..., min_frames=...)` -> per-star lightcurves + ranking (MAD / median error), target/comps excluded, field-median fallback when no comps
- [x] Persistence: scan results table in project DB (schema bump + migration per project-store 07 pattern); query API `scan_results(project)`
- [x] Threshold setting stored in project/settings
- [x] Tests: synthetic subs with one injected variable -> ranked first; constants below threshold; persistence round-trip; no-gi boundary; no network
- [x] Full suite green

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `13f4a97`; test list: `tests/test_variable_scan.py` and `tests/test_variable_scan_boundary.py` (added there); the schema-v7 migration is covered by `tests/test_store_migrations.py`.
