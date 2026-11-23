# 02: Cross-match table persistence in the project

**What to build:** persist the cross-match table per stack in the project DB (schema bump + migration per project-store 07 pattern) with a query API, so repeat views are instant and offline-capable and hr-diagram / variable-scan can consume the table. Demoable: run a crossmatch on a stack of a project, close + reopen the project, table still there and queryable.

**Blocked by:** gaia-crossmatch 01

**Status:** implemented in 221cbed (merged)

- [x] DB schema bump + migration (project-store 07 pattern); store API `save_crossmatch(project, frame_id, rows)` / `load_crossmatch(project, frame_id)` / `list_crossmatched_stacks(project)`
- [x] Rows carry detected x/y, RA/Dec, Gaia source_id, G/BP/RP, parallax + error, pmra/pmdec + errors, matched flag
- [x] Tests: round-trip, migration from previous schema, missing-stack error; no network
- [x] Full suite green

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `221cbed`; test list: tests/test_gaia_persistence.py, tests/test_store_crossmatch.py, tests/test_gaia_boundary.py, tests/test_store_migrations.py. Re-verified during the issue-17 audit: all referenced commits exist on main and the listed targeted tests pass (`uv run pytest <files> -q -n auto`).
