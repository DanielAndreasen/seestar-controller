# 08: Controllers keep a closed db handle after the project is re-opened ("Cannot operate on a closed database")

**Found during:** live guided test (project ngc869, 2026-11-22). After the night was stopped and the project re-opened in the Projects view (the current workaround for the stale stored-data list, observing issue 07), pressing **Solve** in the Enhance view failed with `sqlite3.ProgrammingError: Cannot operate on a closed database.` Root cause: `SolveController` (and siblings) capture the `Project` object in `set_project` (`src/seestar_controller/astrometry/controller.py:261-266`); re-opening the same path produces a NEW `Project` with a fresh db connection, but a controller that missed the follow-up event (or holds the old handle) keeps the old object whose `db` was closed (`store/project.py:273`), so the next store call raises.

**What to build:** controllers must never operate on a stale/closed project handle: either (a) resolve the project lazily from the `ProjectController.current` on every call, or (b) `Project` exposes an `ensure_open()` that reopens the db if closed, called before store access in `_call_store`; plus a regression test that re-opens a project and then solves/cross-matches without ProgrammingError. Also make re-opening the *same* path a no-op (return the existing handle) so the handle swap cannot happen at all.

**Blocked by:** None

**Status:** open — found in live test 2026-11-22

- [ ] Re-open of the same project path returns the existing open handle (no new Project object)
- [ ] Controllers resolve/ensure the live project handle before store calls (or reopen closed db)
- [ ] Test: open project, close handle, re-open, solve_open_stack -> no ProgrammingError
- [ ] Full suite green
