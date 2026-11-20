# 02: Connect the dots — project creation must work end-to-end in the running GUI

**What to build:** issue 01 built the Projects view (list/create/open + ProjectController), but in the running app creating a project still fails for the user. Close the gap end-to-end: from the Projects view "New project" button to a usable current project that unblocks "Start night" in Observe. Cover: project root resolution when `project_root` is unset (fall back to default `~/SeestarProjects`, creating the directory if missing, with a hint pointing to Settings when unwritable); inline error surfacing from `store.create_project` (duplicate/invalid name, unwritable root) in the view's status label; after creation the new project becomes current on ALL shared controllers (observe/viewer/picker/run/lightcurve/period via `ProjectController.attach_controllers`) and is persisted as `last_project`; "Start night" enables when connected + project open. Demoable: fresh settings (no project_root, no last_project), launch GUI, create project "Test" in Projects view -> it appears in the list, becomes current (visible in view/header), survives restart, and Start night enables once the scope is connected.

**Blocked by:** projects-view 01 (done, 52d9967), ui-shell 02 settings (done), project-store 01 (done)

**Status:** done

- [x] Root fallback: unset `project_root` -> default root created on demand; unwritable root -> clear inline error + Settings hint, no crash
- [x] Create flow verified end-to-end in the running GUI (widget test + manual checklist): New project -> store.create_project -> current project event -> all controllers updated -> `last_project` persisted -> restored on restart
- [x] Observe view "Start night" enablement re-checked against the new current project (connected + open project)
- [x] Inline errors for duplicate/invalid name shown in the view (not just logged)
- [x] Tests: pure controller (tmp root, missing root dir creation, error cases, persistence round-trip) + widget tests via ui harness; no network; no gi in pure modules
- [x] Full suite green; ty/ruff clean

## Comments

Implemented in `wt/projects-view-02-create-e2e` (ui/ + tests only):

- `ui/state.py`: `DEFAULT_SETTINGS["project_root"]` is now `""` (unset) on a fresh install — the fallback to the store default (`SEESTAR_PROJECT_ROOT` > `~/SeestarProjects`) lives in `ProjectController.root`, so a fresh install no longer hard-errors in the Settings dialog on a not-yet-existing default root (empty path settings are allowed by validation).
- `ui/project_controller.py`: new `ProjectRootError(OSError)` + `root_error` property — a missing root is NOT an error (created on demand by `create_project` via the store's `mkdir(parents=True)`); an existing root that is not a directory or not writable (`os.access(W_OK|X_OK)`) is an error whose message always carries the "set a (writable) project root in Settings" hint. `create_project` probes the root first and wraps any mid-create `OSError` into `ProjectRootError`; on failure nothing changes (no current, no `last_project`, no event).
- `ui/views.py` (`ProjectsView`): new `_render_root_label()` — red `error` root line with the Settings hint when `root_error`, "not set — using the default … (created on demand; set a project root in Settings)" when the setting is unset, plain root otherwise; re-rendered on rebuild and after a failed create. The create button's inline status label already caught `(ProjectError, ValueError, OSError)`, so `ProjectRootError` surfaces there too.
- `ui/app.py`: `_on_settings_changed` now also calls `project_controller.refresh()` when the `project_root` value changed (tracked via `_last_project_root`), so fixing the root in the Settings dialog re-lists the projects under the new root.
- Create -> current on ALL six shared controllers was already wired by ticket 01 (`attach_controllers` in the app + window); verified end-to-end here: fresh settings -> create "Test" in the Projects view -> row + current badge + banner, root dir created on demand, `last_project` persisted, all six controllers get `set_project`, and a new `SeestarApplication` over the same settings file restores it everywhere. Observe "Start night" enables when connected + project open (window-level widget test with a fake device).
- Tests: `tests/test_ui_project_controller.py` (+6 pure tests: store-default fallback, on-demand root creation, `root_error` None cases, unwritable root -> `ProjectRootError` with Settings hint and no side effects, root-is-a-file, persistence round-trip with restore pushed to six followers); `tests/test_ui_widgets.py` (+4 widget tests: fresh-settings e2e + restart restore, unwritable-root inline error, project_root change re-lists, Start-night enablement in the window with a fake device). `tests/test_ui_state.py` updated for the unset default. No new pure modules (PURE_UI_MODULES unchanged); no network; widget tests skip without gi.
