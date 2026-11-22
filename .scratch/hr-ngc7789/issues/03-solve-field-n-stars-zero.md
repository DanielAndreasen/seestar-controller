# 03: solve-field success always reports `n_stars=0, rms_arcsec=None`

**What to build:** parse the matched-star count and RMS from the solve-field output so a successful local solve reports real numbers instead of the hardcoded `n_stars=0, rms_arcsec=None` in `_solve_with_solve_field` (`src/seestar_controller/astrometry/solve.py:349-350`). solve-field writes `<out>.match` / `.rdls` / prints "solved with N stars, RMS x arcsec"; read what is available (e.g. the `.match` file line count and the field RMS from stdout or the `.wcs`/`.axy` metadata) and fill the `SolveResult` fields.

Evidence: after a successful solve via the Solve panel path, the toast reads "solved: 0 stars (solve-field)" even though the WCS was written and persisted fine (`SolveController` status string built from `result.n_stars`/`result.rms_arcsec`).

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] Successful solve-field run yields `n_stars > 0` and a finite `rms_arcsec` where derivable
- [ ] Failure paths unchanged (still SolveError, nothing persisted)
- [ ] Existing astrometry tests pass + new assertion on parsed counts; full suite green; `ty`/`ruff` clean

## Comments

Found by the hr-ngc7789 driver subagent. Kind: bad-implementation (cosmetic but misleading), severity: low.
