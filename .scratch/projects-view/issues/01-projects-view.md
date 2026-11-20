# 01: Projects view — list / create / open, wired as the app's current project

**What to build:** replace the `ProjectsView` placeholder with a working view: list projects under the configured project root, create a new project by name, open one. Opening a project sets it as the shared current project on all app controllers (observe / enhance / photometry / lightcurve), which unblocks "Start night" in the Observe view. Demoable: launch GUI, create a project in the Projects view, see it become current, and "Start night" becomes enabled once the scope is connected.

**Blocked by:** ui-shell 01 (done), project-store 01 (done), ui-shell 02 settings (done, for project_root)

**Status:** implemented (52d9967)

- [x] Projects view lists projects from `store.list_projects(root)` (name, path, summary info)
- [x] "New project" creates via `store.create_project(name, root)` with inline error on duplicate/invalid name
- [x] Opening a project sets it on the shared controllers (`ObserveController.set_project`, `ViewerController.set_project`, photometry/lightcurve controllers) and shows it as current
- [x] Last opened project persisted in settings and restored on startup
- [x] Pure `ProjectController` (no gi) with listener events; boundary test enforces no-gi; widget tests via ui harness; full suite green

## Comments

Implemented in `52d9967` (docs commit follows). What was built:

- New PURE module `src/seestar_controller/ui/project_controller.py` (no gi; covered by the ui-shell 05 boundary audit — added to `PURE_UI_MODULES` in tests/test_ui_harness.py): `ProjectController(state, root=None)` wraps `store.list_projects/create_project/open_project` against the settings `project_root` (fallback `SEESTAR_PROJECT_ROOT` > `~/SeestarProjects`; explicit `root=` overrides for tests), holds the current `Project`, fires frozen `ProjectEvent(root, projects, current)` on every change, persists the opened project in the new `last_project` setting, and `restore()` re-opens it on startup (clearing stale values). `attach_controllers(observe=/viewer=/picker=/run=/lightcurve=/period=)` pushes `set_project(current)` to all shared controllers on every change + once immediately. Store errors propagate; on failure current/list/setting are unchanged and no event fires.
- `ui/state.py`: `last_project` added to `SETTINGS_KEYS` with `validate_last_project` (empty or existing dir containing `project.json`; `load()` drops stale values) + `EDITABLE_SETTINGS_KEYS` (dialog edits only those). `ui/settings.py` validates it; `SettingsController.update` accepts it and a stale stored value never blocks other saves. `ui/settings_dialog.py` iterates `EDITABLE_SETTINGS_KEYS`.
- `ui/views.py`: `ProjectsView` is now a real widget — current-project banner, dim project-root line, `Gtk.ListBox` of projects (name / path+created / per-row Open button / "current" badge), `Adw.EntryRow` + Create button + inline error/success status label (QueueEditor pattern); re-renders on every controller event. `build_view` forwards `project_controller=` to Projects and Observe views. `ObserveView` follows the project controller and pushes changes into the observe widget.
- `ui/app.py`: `SeestarApplication` owns the shared `project_controller`, calls `restore()` + `attach_controllers(...)` at startup; `SeestarWindow` shares it (attaching itself only when standalone) and passes it + the current project to every view. `observe_view.py` gained a read-only `project` property. Result: "Start night" enables when connected + a project is open (widget-tested).
- Tests: 32 pure tests in `tests/test_ui_project_controller.py` (create/list/open vs tmp root, frozen events, last_project persistence/reload/stale-clearing, error cases, root precedence, attach_controllers, no-gi subprocess + source scan) + 7 widget tests in `tests/test_ui_widgets.py` (list renders, create, open, inline duplicate/invalid/open errors, standalone construct, app startup restore pushing to all 6 controllers, window sharing, Start-night enable). Full suite: **1731 passed + 1 skipped** (baseline 1692 + 1; +39 new), `uv run pytest -q`, 267s.

Assumptions made (also in docs/wiki/assumptions.md 2026-11-20 entry): create = create + open (new project becomes current immediately); switching project does NOT close the previous project's db (WAL multi-connection safe); `last_project` is app-managed, not a dialog row; `ProjectController` lives in `ui/` because it needs both `store` and `ui.state`. No deviations from the issue; no network in tests; live scope untouched.
