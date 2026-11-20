# 01: Projects view — list / create / open, wired as the app's current project

**What to build:** replace the `ProjectsView` placeholder with a working view: list projects under the configured project root, create a new project by name, open one. Opening a project sets it as the shared current project on all app controllers (observe / enhance / photometry / lightcurve), which unblocks "Start night" in the Observe view. Demoable: launch GUI, create a project in the Projects view, see it become current, and "Start night" becomes enabled once the scope is connected.

**Blocked by:** ui-shell 01 (done), project-store 01 (done), ui-shell 02 settings (done, for project_root)

**Status:** ready-for-agent

- [ ] Projects view lists projects from `store.list_projects(root)` (name, path, summary info)
- [ ] "New project" creates via `store.create_project(name, root)` with inline error on duplicate/invalid name
- [ ] Opening a project sets it on the shared controllers (`ObserveController.set_project`, `ViewerController.set_project`, photometry/lightcurve controllers) and shows it as current
- [ ] Last opened project persisted in settings and restored on startup
- [ ] Pure `ProjectController` (no gi) with listener events; boundary test enforces no-gi; widget tests via ui harness; full suite green

## Comments

(implementor fills in)
