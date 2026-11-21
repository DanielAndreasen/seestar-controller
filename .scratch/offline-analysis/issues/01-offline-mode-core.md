# 01: Offline mode core — open project without link, gate scope actions

**What to build:** the offline mode itself: an "Open project (offline)" action in the app shell that opens a project directory from the project store WITHOUT starting the link layer or any session; an app/window mode flag (`offline` vs `connected`) with a visible offline banner; all scope-dependent actions (connect, goto/slew, live preview, start night, pause/resume/stop) disabled or hidden in offline mode. Demoable: launch GUI, open an existing project offline, banner shows offline, scope buttons disabled, project data visible.

**Blocked by:** None (can start immediately)

**Status:** implemented in 1818a75+0ddf1e9 (merged)

- [ ] `open_project_offline(path)` path in ui-shell: opens store project, sets mode flag, never touches link/
- [ ] Mode flag on app + window; offline banner/indicator in the UI
- [ ] Scope actions gated on mode (disabled with tooltip in offline mode)
- [ ] Widget tests via the ui harness (link layer mocked to fail — offline open must still work); full suite green

## Comments

(implementor fills in)
