# 16: Deleting a project is promised by the project-store spec but implemented nowhere

**What to build:** resolve the spec conflict and provide the capability: project-store spec user story 9 ("list and delete old projects") has no implementation, while the projects-view spec explicitly lists "Deleting/renaming projects" as Out of Scope. Either implement project deletion (store-level `delete_project` plus UI affordance) or move story 9 to Out of Scope and align both specs.

Evidence: `.scratch/project-store/spec.md` user story 9 vs `grep -rn "def delete" src/seestar_controller/` → only preset deletion (`enhance/persistence.py`, `store/presets.py`); no delete_project in `store/`; none of the 7 project-store issues covers deletion; `.scratch/projects-view/spec.md` declares it out of scope.

**Blocked by:** None (can start immediately)

**Status:** implemented in 4a38a53 (pending review)

- [x] Decision recorded in both specs (implement vs descope) and acted on: if implemented, `store.delete_project` removes the project directory/DB safely (refuse when open, tests) and the Projects view offers deletion; if descoped, story 9 is moved to Out of Scope in the project-store spec
- [x] Full suite green; ty + ruff clean

## Comments

Audit finding (kind: missing-feature, severity: low). Spec-vs-spec conflict — nothing implements it either way.

Implemented (not descoped) in 4a38a53: `store.delete_project(path)` (validates the directory is a project, refuses with the new `ProjectOpenError` while any `open_project` handle is live, `shutil.rmtree` otherwise) + `ProjectController.delete_project` (closes the current project first when it is the deleted one, clears `last_project`, notifies followers with `None`, rolls back on refusal) + a destructive Delete button per row in the Projects view. Decision recorded in `.scratch/project-store/spec.md` (story 9 implemented) and `.scratch/projects-view/spec.md` (deletion removed from Out of Scope).

Test/lint results (branch wt/app-improvement-16, commit 4a38a53): targeted suites green with gi present — `uv run --extra gui pytest tests/test_store_project.py tests/test_ui_project_controller.py tests/test_ui_widgets.py tests/test_ui_projects_stack_subs.py` (14 new delete tests included, all pass; store+controller suites 118 passed). Also green: test_store_sessions/frames/csv/migrations, test_ui_state, test_observing_widgets, test_offline_analysis_widgets except 3 pre-existing gi-environment failures (stale-session dialog x2, offline photometry run x1) that fail identically on the base commit ae31075 (other tickets 02/04). `uv tool run ruff check`: all checks passed. `uv tool run ty check`: 107 diagnostics, byte-identical to base (all pre-existing `gi.repository` member-resolution artifacts of the isolated ty env; no new diagnostics from this change). Full suite not run here per ticket rules — coordinator runs it at merge time.
