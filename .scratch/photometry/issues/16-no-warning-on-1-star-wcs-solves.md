# Issue 16: WCS solve quality not gated — all 512 frame_wcs rows have n_stars=1, rms_arcsec NULL; reprojection errors reach hundreds of px, only the ≤10 px issue-12 refinement rescues frames

**Status:** needs-triage

## Found while
Headless lightcurve run 2026-11-22 (vw-cep): every one of the 512 batch-solved subs has `n_stars=1` and `rms_arcsec IS NULL` in frame_wcs; reprojection error for the target reached ~268 px on frame 1 (rescued only because the star was within the 10 px refinement window on most frames; 146 frames lost a pick entirely). Nothing warns the user that these solves are low-confidence.

## Expected
- `session_wcs_coverage` / the Run-tab WCS label (and the batch-solve report) surface solve quality: count of frames with n_stars below a threshold (e.g. <8) or rms_arcsec NULL / above a threshold (e.g. >5 arcsec), e.g. "WCS: 512 of 512 solved (low-confidence: 512)".
- Batch solve (`solve_session_unsolved`) marks low-confidence solves in its report (`SessionSolveReport.low_confidence_ids`).
- Optionally re-solve low-confidence frames with a different solver/index or wider scale hints.

## Scope
`src/seestar_controller/astrometry/session.py` (report fields), `photometry/run.py` (`wcs_coverage` summary), `photometry/results_view.py` (label), tests.
