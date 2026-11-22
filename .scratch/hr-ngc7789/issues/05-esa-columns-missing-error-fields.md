# 05: `ESA_GAIA_COLUMNS` lacks the parallax/pm error columns — ESA fallback yields empty H-R

**What to build:** add `parallax_error` / `pmra_error` / `pmdec_error` (archive spellings `parallax_error` / `pmra_error` / `pmdec_error` on `gaiadr3.gaia_source`) to `ESA_GAIA_COLUMNS` in `src/seestar_controller/gaia/query.py:150-160` so a successful ESA-mirror fallback carries the same 12 fields as the VizieR dialect. `_COLUMN_ALIASES` (:163-178) already maps the spellings; `GaiaStar`/`CrossmatchRow`/store already carry them after issue 02.

Evidence: ESA dialect selects only 9 columns, so even a working ESA fallback yields `parallax_error=None` for every row → `hr_diagram.hr_points` SNR cut drops all rows → H-R page empty again. Found by the hr-ngc7789 driver re-run (it had to seed the cache with a hand-written 12-column ADQL to get 44 H-R points).

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] `build_cone_query(url=ESA...)` selects all 12 columns incl. the three error fields
- [ ] Parsed ESA responses populate the error fields on `GaiaStar`
- [ ] Tests updated/added; full suite green; `ty`/`ruff` clean

## Comments

Found by the hr-ngc7789 driver re-run. Kind: missing feature, severity: high (defeats issue 02 whenever the fallback endpoint is the one that answers).
