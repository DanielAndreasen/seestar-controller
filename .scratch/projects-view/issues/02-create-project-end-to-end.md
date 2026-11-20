# 02: Connect the dots — project creation must work end-to-end in the running GUI

**What to build:** issue 01 built the Projects view (list/create/open + ProjectController), but in the running app creating a project still fails for the user. Close the gap end-to-end: from the Projects view "New project" button to a usable current project that unblocks "Start night" in Observe. Cover: project root resolution when `project_root` is unset (fall back to default `~/SeestarProjects`, creating the directory if missing, with a hint pointing to Settings when unwritable); inline error surfacing from `store.create_project` (duplicate/invalid name, unwritable root) in the view's status label; after creation the new project becomes current on ALL shared controllers (observe/viewer/picker/run/lightcurve/period via `ProjectController.attach_controllers`) and is persisted as `last_project`; "Start night" enables when connected + project open. Demoable: fresh settings (no project_root, no last_project), launch GUI, create project "Test" in Projects view -> it appears in the list, becomes current (visible in view/header), survives restart, and Start night enables once the scope is connected.

**Blocked by:** projects-view 01 (done, 52d9967), ui-shell 02 settings (done), project-store 01 (done)

**Status:** ready-for-agent

- [ ] Root fallback: unset `project_root` -> default root created on demand; unwritable root -> clear inline error + Settings hint, no crash
- [ ] Create flow verified end-to-end in the running GUI (widget test + manual checklist): New project -> store.create_project -> current project event -> all controllers updated -> `last_project` persisted -> restored on restart
- [ ] Observe view "Start night" enablement re-checked against the new current project (connected + open project)
- [ ] Inline errors for duplicate/invalid name shown in the view (not just logged)
- [ ] Tests: pure controller (tmp root, missing root dir creation, error cases, persistence round-trip) + widget tests via ui harness; no network; no gi in pure modules
- [ ] Full suite green; ty/ruff clean

## Comments

(implementor fills in)
