# 19: check-star-plot issue 01 claims "migration-free storage" but a v4→v5 table rebuild was required

**What to build:** reword the issue text so it matches what the code does.

Evidence: `.scratch/check-star-plot/issues/01-check-star-core.md` says the role vocabulary was extended "with migration-free storage (picks table role column)", but `src/seestar_controller/store/schema.py:105-106` documents "4 -> 5 — check-star-plot ticket 01: widen the stars.role CHECK to also allow 'check' ... the stars table is REBUILT" (`_migrate_4_to_5` at :481-489, migration map :528). Functional behavior is fine (ROLE_CHECK persists in the role column, tests green) — only the "migration-free" wording is false.

**Blocked by:** None (can start immediately)

**Status:** implemented in a537b5d (pending review)

- [x] The issue checkbox is reworded to "stored in the same role column via the v4→v5 CHECK-widening migration"
- [x] No code changes; docs-only ticket

## Comments

Audit finding (kind: incorrect-spec-claim, severity: low).

Implemented in a537b5d: reworded the acceptance line in `.scratch/check-star-plot/issues/01-check-star-core.md` from "migration-free storage (picks table role column)" to "stored in the same role column via the v4→v5 CHECK-widening migration", matching `src/seestar_controller/store/schema.py` (4 -> 5 rebuilds the `stars` table to widen the role CHECK). Docs-only; no code changed, so no targeted tests named. `uv tool run ty check` -> All checks passed! `uv tool run ruff check` -> All checks passed!
