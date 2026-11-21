# 02: Cross-match table persistence in the project

**What to build:** persist the cross-match table per stack in the project DB (schema bump + migration per project-store 07 pattern) with a query API, so repeat views are instant and offline-capable and hr-diagram / variable-scan can consume the table. Demoable: run a crossmatch on a stack of a project, close + reopen the project, table still there and queryable.

**Blocked by:** gaia-crossmatch 01

**Status:** implemented in 221cbed (merged)

- [ ] DB schema bump + migration (project-store 07 pattern); store API `save_crossmatch(project, frame_id, rows)` / `load_crossmatch(project, frame_id)` / `list_crossmatched_stacks(project)`
- [ ] Rows carry detected x/y, RA/Dec, Gaia source_id, G/BP/RP, parallax + error, pmra/pmdec + errors, matched flag
- [ ] Tests: round-trip, migration from previous schema, missing-stack error; no network
- [ ] Full suite green

## Comments

(implementor fills in)
