# 05: `ESA_GAIA_COLUMNS` lacks the parallax/pm error columns — ESA fallback yields empty H-R

**What to build:** add `parallax_error` / `pmra_error` / `pmdec_error` (archive spellings `parallax_error` / `pmra_error` / `pmdec_error` on `gaiadr3.gaia_source`) to `ESA_GAIA_COLUMNS` in `src/seestar_controller/gaia/query.py:150-160` so a successful ESA-mirror fallback carries the same 12 fields as the VizieR dialect. `_COLUMN_ALIASES` (:163-178) already maps the spellings; `GaiaStar`/`CrossmatchRow`/store already carry them after issue 02.

Evidence: ESA dialect selects only 9 columns, so even a working ESA fallback yields `parallax_error=None` for every row → `hr_diagram.hr_points` SNR cut drops all rows → H-R page empty again. Found by the hr-ngc7789 driver re-run (it had to seed the cache with a hand-written 12-column ADQL to get 44 H-R points).

**Blocked by:** None (can start immediately)

**Status:** completed — reviewed (APPROVE) and merged to main in 3cfa7ff

- [x] `build_cone_query(url=ESA...)` selects all 12 columns incl. the three error fields
- [x] Parsed ESA responses populate the error fields on `GaiaStar`
- [x] Tests updated/added; full suite green; `ty`/`ruff` clean

## Comments

Found by the hr-ngc7789 driver re-run. Kind: missing feature, severity: high (defeats issue 02 whenever the fallback endpoint is the one that answers).

Implemented on branch `wt/hr-ngc7789-05` (worktree `hr-ngc7789-05`), commit `dda71df`:

- `gaia/query.py`: `ESA_GAIA_COLUMNS` now selects the same 12 fields as `GAIA_COLUMNS`, adding the archive spellings `parallax_error` / `pmra_error` / `pmdec_error` in SELECT order (after `parallax` / `pmra` / `pmdec`). `build_cone_query(url=ESA_GAIA_TAP_SYNC_URL)` therefore emits all 12 quoted columns, and `_COLUMN_ALIASES` (already mapping the archive spellings) makes `parse_gaia_response` populate `GaiaStar.parallax_error` / `pmra_error` / `pmdec_error` from a successful ESA fallback. No other code change was needed — `GaiaStar`, the cache row serialization, and the crossmatch/store already carry the fields since issue 02.
- Tests (`tests/test_gaia_query.py`): `ESA_METADATA` / `esa_payload` now build the 12-column ESA-archive payload shape (error cells from the optional `plx_err` / `pmra_err` / `pmde_err` row keys); `test_build_cone_query_esa_mirror_uses_the_gaia_archive_dialect` asserts `len(ESA_GAIA_COLUMNS) == len(GAIA_COLUMNS) == 12` and the three error spellings in the ADQL; the VizieR→ESA fallback tests assert the parsed `GaiaStar`s carry the error values (0.03 / 0.04 / 0.05) and that the cached fallback result round-trips them.

Verification (worktree `~/.vibe/worktrees/seestar-controller-dec7d727fea1/hr-ngc7789-05`): `uv tool run ty check` → all checks passed; `uv tool run ruff check` → all checks passed; `uv run pytest -q -n auto` → 2653 passed, 20 skipped, 1 failed — the single failure is `test_visibility_planner.py::test_every_visibility_module_is_pure_or_widget_and_the_public_api_is_exported` (`ModuleNotFoundError: No module named 'gi'`), one of the two documented no-gi environmental failures; verified identical on pristine main (63a5ec6) in a fresh no-gi venv (same 1 failed / 2653 passed / 20 skipped full-suite result).
