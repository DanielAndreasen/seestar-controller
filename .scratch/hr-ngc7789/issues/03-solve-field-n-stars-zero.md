# 03: solve-field success always reports `n_stars=0, rms_arcsec=None`

**What to build:** parse the matched-star count and RMS from the solve-field output so a successful local solve reports real numbers instead of the hardcoded `n_stars=0, rms_arcsec=None` in `_solve_with_solve_field` (`src/seestar_controller/astrometry/solve.py:349-350`). solve-field writes `<out>.match` / `.rdls` / prints "solved with N stars, RMS x arcsec"; read what is available (e.g. the `.match` file line count and the field RMS from stdout or the `.wcs`/`.axy` metadata) and fill the `SolveResult` fields.

Evidence: after a successful solve via the Solve panel path, the toast reads "solved: 0 stars (solve-field)" even though the WCS was written and persisted fine (`SolveController` status string built from `result.n_stars`/`result.rms_arcsec`).

**Blocked by:** None (can start immediately)

**Status:** completed — reviewed (approved) and merged to main in 81f1f89

- [x] Successful solve-field run yields `n_stars > 0` and a finite `rms_arcsec` where derivable
- [x] Failure paths unchanged (still SolveError, nothing persisted)
- [x] Existing astrometry tests pass + new assertion on parsed counts; full suite green; `ty`/`ruff` clean

## Comments

Found by the hr-ngc7789 driver subagent. Kind: bad-implementation (cosmetic but misleading), severity: low.

Implemented in `85d9830` on branch `wt/hr-ngc7789-03` (worktree `hr-ngc7789-03`):

- `src/seestar_controller/astrometry/solve.py` — `_solve_with_solve_field` now parses the solve's
  matched-star count and RMS instead of hardcoding `n_stars=0, rms_arcsec=None`:
  - `n_stars` = row count of the `<out>.match` FITS BINTABLE solve-field writes next to the
    solution (`_match_file_rows`, lazy astropy, None when missing/unreadable), falling back to
    the `(\d+) stars` count in the stdout/stderr line (`_parse_solve_field_output`, last match
    wins), else 0;
  - `rms_arcsec` = the `RMS <x> arcsec` value from the stdout/stderr line (finite check), else
    None — exactly "where derivable";
  - both are read inside the temp dir while the solve-field outputs still exist; failure paths
    (nonzero exit, timeout, missing/non-celestial `.wcs`) still raise `SolveError` untouched.
  - `SolveResult`/module docstrings updated to describe the parsed values.
- `tests/test_astrometry_solve.py` — the fake `solve-field` binary now also writes a `.match`
  BINTABLE (one row per injected star) and prints `Field 1: solved with 10 stars, RMS 0.42 arcsec`
  (env switches `FAKE_SOLVE_NO_MATCH` / `FAKE_SOLVE_NO_STDOUT_STATS`); new assertions in
  `test_solve_frame_solve_field_binary_success` (`n_stars == len(STARS)`,
  `rms_arcsec == approx(0.42)`), two new fallback tests (stdout-only, .match-only → `rms_arcsec
  is None`), and a direct unit test of `_parse_solve_field_output` (last-match-wins, `RMS:`
  variants, SExtractor `RMS:` without arcsec ignored).

Verification: `uv tool run ty check` and `uv tool run ruff check` clean;
`uv run pytest -q tests/test_astrometry_solve.py tests/test_astrometry_boundary.py
tests/test_astrometry_store.py` → 52 passed; full suite `uv run pytest -q -n auto` → 2640
passed, 20 skipped, 1 failed — the failure
(`test_visibility_planner.py::test_every_visibility_module_is_pure_or_widget_and_the_public_api_is_exported`,
`ModuleNotFoundError: No module named 'gi'`) is pre-existing/environmental: the fresh worktree
venv has no `gi` (the main checkout's venv does) and the same test fails identically on the base
commit with my changes stashed.
