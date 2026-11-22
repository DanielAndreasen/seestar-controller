# 03: Stack subs into a stack offline

**What to build:** a "Stack subs" action on session rows in the Stored-data browser (next to "Stitch mosaic") that combines a session's subs into a kind='stack' FITS written back to the project (median combine; scope subs are already aligned, so no registration — document that assumption), after which all stack-based views (Enhance viewer, mosaic, report) work on the result. Demoable: NGC7789 offline -> Stack subs -> new stack appears in Enhance viewer list and opens.

**Blocked by:** None (can start immediately)

**Status:** implemented in 25f50a6 (merged)

- [x] Pure stacking function (median combine of subs of one session -> stack FITS via store, kind='stack'); per-frame failures skipped, never crash
- [x] UI: "Stack subs" button per session row in Projects view Stored-data browser, worker thread + inline status, writes into project
- [x] Tests: pure (synthetic subs -> valid stack readable by list_project_stacks, header sane) + gi-gated widget test; full suite green; ty + ruff clean

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `25f50a6`; test list: `tests/test_stacking_boundary.py`, `tests/test_stack_subs.py`, `tests/test_ui_projects_stack_subs.py` (added there).
