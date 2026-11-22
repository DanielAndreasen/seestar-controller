# 08: Controllers keep a closed db handle after the project is re-opened ("Cannot operate on a closed database")

**Found during:** live guided test (project ngc869, 2026-11-22). After the night was stopped and the project re-opened in the Projects view (the current workaround for the stale stored-data list, observing issue 07), pressing **Solve** in the Enhance view failed with `sqlite3.ProgrammingError: Cannot operate on a closed database.` Root cause: `SolveController` (and siblings) capture the `Project` object in `set_project` (`src/seestar_controller/astrometry/controller.py:261-266`); re-opening the same path produces a NEW `Project` with a fresh db connection, but a controller that missed the follow-up event (or holds the old handle) keeps the old object whose `db` was closed (`store/project.py:273`), so the next store call raises.

**What to build:** controllers must never operate on a stale/closed project handle: either (a) resolve the project lazily from the `ProjectController.current` on every call, or (b) `Project` exposes an `ensure_open()` that reopens the db if closed, called before store access in `_call_store`; plus a regression test that re-opens a project and then solves/cross-matches without ProgrammingError. Also make re-opening the *same* path a no-op (return the existing handle) so the handle swap cannot happen at all.

**Blocked by:** None

**Status:** completed — reviewed (APPROVE), merged to main in e0552f7

- [x] Re-open of the same project path returns the existing open handle (no new Project object)
- [x] Controllers resolve/ensure the live project handle before store calls (or reopen closed db)
- [x] Test: open project, close handle, re-open, solve_open_stack -> no ProgrammingError
- [x] Full suite green

## Comments

Implemented on branch `wt/fix-ps-08` (worktree `fix-ps-08`), commit `01cdd74`:

- `store/project.py`: `open_project` on an already-open path (resolved-path scan of `_OPEN_PROJECTS`) returns the existing live handle — no second connection, so the handle swap cannot happen; `fresh=True` opts out for callers that need a second thread-local connection. New `Project.ensure_open()` reconnects a closed handle to `db.sqlite` (WAL + foreign keys via the shared `_connect_db`) and re-registers it as open.
- All ten `_call_store` implementations (astrometry Solve/Drift, lightcurve, fold, moving-objects, frame-quality trends, photometry run, hr-diagram, gaia, variable-scan) call `project.ensure_open()` immediately before the store callable, on the thread the callable runs on (sqlite thread-affinity preserved).
- `observing/report_panel.py` opens its worker connection with `fresh=True` (a plain re-open now returns the main-thread handle, which is thread-bound).
- Tests: `tests/test_store_project.py` (same-path re-open identity, `fresh=True`, `ensure_open`), `tests/test_astrometry_view.py` (close handle, re-open path, `solve_open_stack` — no ProgrammingError, both `_call_store` branches); delete-refusal / corrupt-project tests adapted to the single-handle semantics.

Verification: `uv run pytest -q -n auto` → 2883 passed, 2 skipped (environmental: missing `reproject`, gui-launch guard); `uv tool run ruff check` → clean; `uv tool run ty check` → 108 diagnostics (pre-existing gi-stub noise, unchanged from main).
