# Issue 18: CSV export `target` column is empty even though star_picks.target is set

**Status:** needs-triage

## Found while
Headless lightcurve run 2026-11-22 (vw-cep): exported CSV header `jd,utc,mag,mag_err,session,target` had empty target on every row; `project.config["target"]` is null while `store.stars.load_star_picks` has the target pick labeled "VW Cep". The exporter's `_target_name` reads only the config.

## Expected
- The CSV target column falls back to the saved target pick's label (star_picks target) when `config["target"]` is null. Test: project with picks but no config target → CSV rows carry the pick label.

## Scope
`src/seestar_controller/store/csv_export.py` (or `analysis/export.py` wrapper), tests in `tests/test_*csv*.py`.
