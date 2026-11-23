# 01: Check-star role in the photometry core (pure)

**What to build:** extend the photometry core with a check-star role: `StarPickSpec` role gains `check`; the check star is EXCLUDED from the comp ensemble (ensemble = mean of comps only); per frame, compute the check star's differential magnitude + error against the comp ensemble exactly like the target (same error propagation), and the target-minus-check curve. Persist the check-star choice in the project (store.stars). Demoable: synthetic frames where a cloud dims everything -> target and check move together, target-minus-check stays flat within errors; check excluded from ensemble math.

**Blocked by:** None (photometry 01-04, project-store 04 done)

**Status:** implemented in cad0b2e (merged)

- [x] Pure change in photometry (picking/measure/run), no gi; role vocabulary extended in store/stars.py (ROLE_CHECK), stored in the same role column via the v4→v5 CHECK-widening migration
- [x] `measure_frame_data` / run report expose per-frame check differential mag + err and target-minus-check; ensemble math provably excludes the check
- [x] Check-star choice persisted per project (store.stars save/load round-trip)
- [x] Tests: synthetic cloud-dimming frames -> target & check co-move, target-minus-check flat within errors; ensemble excludes check; persistence round-trip; no-gi boundary
- [x] Full suite green

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `cad0b2e`; test list: tests/test_photometry_check_star.py, tests/test_photometry_picking.py, tests/test_store_stars.py, tests/test_store_migrations.py. Re-verified during the issue-17 audit: all referenced commits exist on main and the listed targeted tests pass (`uv run pytest <files> -q -n auto`).
