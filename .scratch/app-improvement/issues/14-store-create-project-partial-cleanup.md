# 14: `create_project` leaves a poisoned partial directory on failure; `open_project` can leak the db handle

**What to build:** make project creation all-or-nothing and project opening exception-safe.

Evidence: `src/seestar_controller/store/project.py:287-311` — after collision-guarded mkdir, subdirs, and sqlite init, an OSError writing `project.json` (:310) leaves `<root>/<slug>/` with `db.sqlite` but no `project.json`; a retry then raises `ProjectExistsError` and `open_project` reports "missing project.json" — the name is blocked forever by a half-initialized directory. Also `open_project`'s `project.save_config()` (:393-396) sits outside the try/except that closes `db` on error (:370-388), so the connection is not explicitly closed on that path.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] A failed `create_project` removes the partial directory (all-or-nothing), and the name can be reused afterwards
- [ ] `open_project` closes the db connection on any exception (try/finally or broader except)
- [ ] Tests in `tests/test_store_project.py` (or equivalent) cover both paths; full suite green; ty + ruff clean

## Comments

Audit finding (kind: bad-implementation, severity: low). Covered-by pre-audit: `.scratch/project-store/spec.md`.
