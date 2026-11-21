# 01: Offline mode core — open project without link, gate scope actions

**What to build:** the offline mode itself: an "Open project (offline)" action in the app shell that opens a project directory from the project store WITHOUT starting the link layer or any session; an app/window mode flag (`offline` vs `connected`) with a visible offline banner; all scope-dependent actions (connect, goto/slew, live preview, start night, pause/resume/stop) disabled or hidden in offline mode. Demoable: launch GUI, open an existing project offline, banner shows offline, scope buttons disabled, project data visible.

**Blocked by:** None (can start immediately)

**Status:** implemented in 1818a75+0ddf1e9 (merged)

- [x] `open_project_offline(path)` path in ui-shell: opens store project, sets mode flag, never touches link/
- [x] Mode flag on app + window; offline banner/indicator in the UI
- [x] Scope actions gated on mode (disabled with tooltip in offline mode)
- [x] Widget tests via the ui harness (link layer mocked to fail — offline open must still work); full suite green

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commits `1818a75` + `0ddf1e9`; test list: `tests/test_offline_mode.py` and `tests/test_offline_mode_widgets.py` (added there), plus `tests/test_ui_harness.py`.
