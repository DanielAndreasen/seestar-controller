# Spec: projects-view

Status: ready-for-agent

## Problem Statement

The Projects view in the GUI is still a placeholder, so there is no way to create or open a project from the app. Because "Start night" in the Observe view requires an open project, the whole observing flow is currently unusable in the GUI even though the store layer fully supports projects.

## Solution

Implement the Projects view: list existing projects under the configured project root, create a new project, and open one. Opening a project makes it the shared "current project" for the app's controllers (observe, enhance, photometry, lightcurve), which unblocks "Start night" and the other project-dependent views.

## User Stories

1. As a user, I want to see all projects under my configured project root, so that I can pick the campaign I want to continue.
2. As a user, I want to create a new project by name from the GUI, so that I can start a new observing campaign without touching the filesystem.
3. As a user, I want to open an existing project, so that its sessions, frames, picks, and lightcurve become the current project everywhere in the app.
4. As a user, I want the currently open project to be visible (e.g. in the Projects view and/or window header), so that I always know where new data will be saved.
5. As a user, I want a clear inline error when project creation fails (duplicate name, invalid name, unwritable root), so that I can fix the input.
6. As a user, I want the last opened project to be remembered across app restarts (via settings), so that I land back in my campaign.

## Implementation Decisions

- Pure layer: a `ProjectController` (new module, e.g. `store/project_controller.py` or `ui/state.py`-adjacent pure controller following the existing controller pattern) wrapping `store.create_project` / `open_project` / `list_projects`, holding the current `Project`, persisting the last-opened project path in settings (`project_root` already exists in settings; add e.g. `last_project`), and notifying listeners with a frozen event on change. No `gi` imports.
- Widget layer: replace the `ProjectsView` placeholder in `ui/views.py` with a real widget: a list of projects (name, path, last-modified/session count from `ProjectSummary`), a "New project" name entry + button, and an "Open" action per row (or select + Open button). Inline status label for errors, same pattern as `QueueEditor`.
- Wiring: `SeestarApplication` owns one shared `ProjectController`; `SeestarWindow` passes it to `build_view(...)` for every view that needs the current project (Observe, Enhance, Photometry, Lightcurve) and calls `set_project(...)` on the existing shared controllers (`ObserveController.set_project`, `ViewerController.set_project`, etc.) whenever the current project changes. On startup, if settings has a `last_project` that still exists, open it automatically.
- Project root comes from settings (`project_root`, default `DEFAULT_PROJECT_ROOT`); if unset, fall back to the default root and show a hint pointing to Settings.
- Store API is used as-is (`create_project(name, root)`, `open_project(path)`, `list_projects(root)`); no store schema changes.

## Testing Decisions

- Pure controller tests: create/list/open against a tmp root, current-project events, last-project persistence in settings, error cases (duplicate/invalid name, missing path) — no GTK, no network.
- Widget tests via the existing ui harness (importorskip gi): list renders, create button calls controller, open selects project, error shown inline.
- Boundary test: pure controller module imports no `gi` (subprocess + source scan, mirroring ui-shell 05).
- Full suite must stay green.

## Out of Scope

- Deleting/renaming projects, project settings editing beyond name, multi-root support, cloud sync.

## Further Notes

- This unblocks the Observe view's "Start night" button (requires connected + open project).
- The 11 spec-only ideas in `.scratch/` (astrometry, gaia-crossmatch, etc.) remain untouched.
