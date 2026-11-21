# 14: `create_project` leaves a poisoned partial directory on failure; `open_project` can leak the db handle

**What to build:** make project creation all-or-nothing and project opening exception-safe.

Evidence: `src/seestar_controller/store/project.py:287-311` — after collision-guarded mkdir, subdirs, and sqlite init, an OSError writing `project.json` (:310) leaves `<root>/<slug>/` with `db.sqlite` but no `project.json`; a retry then raises `ProjectExistsError` and `open_project` reports "missing project.json" — the name is blocked forever by a half-initialized directory. Also `open_project`'s `project.save_config()` (:393-396) sits outside the try/except that closes `db` on error (:370-388), so the connection is not explicitly closed on that path.

**Blocked by:** None (can start immediately)

**Status:** implemented in 9c176ea (pending review)

- [x] A failed `create_project` removes the partial directory (all-or-nothing), and the name can be reused afterwards
- [x] `open_project` closes the db connection on any exception (try/finally or broader except)
- [x] Tests in `tests/test_store_project.py` (or equivalent) cover both paths; full suite green; ty + ruff clean

## Comments

Audit finding (kind: bad-implementation, severity: low). Covered-by pre-audit: `.scratch/project-store/spec.md`.

Implemented in 9c176ea on `wt/app-improvement-14`:

- `create_project` (src/seestar_controller/store/project.py): everything after the collision-guarded `mkdir` is wrapped in `try/except BaseException` that `shutil.rmtree(project_dir, ignore_errors=True)`s the partial directory and re-raises — all-or-nothing, name stays reusable. Docstrings updated (module + function).
- `open_project`: validation, migration, and the `project.json` schema_version mirror write now sit in one `try` whose `finally` closes the db unless the open succeeded (`opened` flag), so no failure path — including a failing `save_config()` — leaks the connection.
- tests/test_store_project.py: +5 tests (73 total in the file): failed `project.json` write (OSError and KeyboardInterrupt at `os.replace`) and failed db init both remove the partial dir and allow immediate reuse; failing mirror write during open and mid-validation failure (missing tables, tracked via patched `sqlite3.connect`) both leave the connection closed (ProgrammingError on reuse).
- Targeted tests: `uv run pytest tests/test_store_project.py -q` -> 73 passed; also ran the other store suites + project-controller tests (364 passed, 1 gi-skip in the fresh worktree venv) and the gi-gated `test_ui_projects_stack_subs.py`/`test_ui_widgets.py` against the main-checkout venv (41 passed).
- `uv tool run ty check` -> All checks passed!  `uv tool run ruff check` -> All checks passed!
