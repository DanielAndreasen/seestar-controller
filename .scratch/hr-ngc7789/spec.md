# HR diagram from NGC7789 — GUI-path verification

**Status:** completed — all 6 issues fixed, reviewed (approved) and merged to main; driver re-run succeeded end-to-end (ESA fallback over network, H-R 44 pts / CMD 46 pts); driver + guide + artifacts merged in b5f7ed6

Driving the exact GUI controller path headlessly (ViewerController.open_stack → SolveController.solve_open_stack → CrossmatchController.run_crossmatch → HrDiagramController.refresh) on the real project `/home/daniel/SeestarProjects/ngc7789` (session 20261120T213247325993Z, frames 1/2 subs + 3 stack, all pre-solved) to produce an H-R/CMD diagram and a written guide ("from observation to H-R diagram"). The run succeeded for the CMD (46 points) but surfaced three defects, filed as tickets under `.scratch/hr-ngc7789/issues/`:

1. Gaia cross-match hard-fails when the hardcoded VizieR TAP endpoint is down — no mirror fallback.
2. Cross-match never fetches/stores parallax/pm error columns, so the H-R page is always empty.
3. solve-field success reports `n_stars=0, rms=None` (hardcoded), so the Solve toast is misleading.

Driver script + artifacts + guide live on branch `wt/hr-ngc7789` (worktree `~/.vibe/worktrees/seestar-controller-dec7d727fea1/hr-ngc7789`, `docs/hr-ngc7789/`): `headless_hr_ngc7789.py`, `from-observation-to-hr-diagram.md`, `artifacts/`.

## Goal

After the fixes merge, re-run the same driver on the NGC7789 project and produce a non-empty H-R page (parallax SNR cut must pass for real members) plus the final guide, then merge the result.
