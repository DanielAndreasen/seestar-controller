# 01: Frame-quality metrics core + flagging + per-session storage

**What to build:** per-sub-frame quality metrics (FWHM, background median/std, detected star count) computed from stored sub-frames, robust MAD z-score flagging per session, and persistence of metrics + flags in the project. Demoable: run scoring over a session's subs in a project; bad frames (injected blurry/cloudy) are flagged, good ones pass; metrics queryable per session.

**Blocked by:** project-store 03/05 (done), photometry 03 (done)

**Status:** implemented in 82ee1a9 (merged)

- [x] Pure `frame_quality/` package (no gi): `score_frame(fits_path or array) -> FrameMetrics(fwhm, bg_median, bg_std, star_count)`; star detection via photutils DAOStarFinder (or documented fallback), background via sigma-clipped stats
- [x] `score_session(project, session_id)` scores all subs of a session; `flag_outliers(metrics, mad_threshold=3.0)` robust z-score per metric per session -> flagged set
- [x] Metrics + flags persisted in project DB (schema bump + migration per project-store 07 pattern); query API `session_quality(project, session_id)` -> metrics table + summary (median FWHM, % rejected)
- [x] Tests: synthetic FITS subs with one injected blurry/cloudy frame -> flagged, others pass; summary math; migration; no-gi boundary test
- [x] Full suite green

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `82ee1a9`; test list: tests/test_frame_quality.py, tests/test_store_quality.py, tests/test_store_migrations.py. Re-verified during the issue-17 audit: all referenced commits exist on main and the listed targeted tests pass (`uv run pytest <files> -q -n auto`).
