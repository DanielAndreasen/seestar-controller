# 03: Recent projects list + offline indicator polish

**What to build:** the app remembers recently opened projects (persisted in the app config dir, e.g. `~/.config/seestar-controller/recent.json`, atomic write, capped list) and offers them in the open dialog / a "Recent projects" submenu for one-click reopening; the offline banner is clearly visible and states that no telescope is connected. Demoable: open a project offline, quit, relaunch — project appears in recent list, one click reopens it offline.

**Blocked by:** offline-analysis 01

**Status:** implemented in 3d6a6a4 (merged)

- [x] Recent-projects persistence in app config dir (atomic, capped, most-recent-first, dedup)
- [x] UI: recent projects menu/dialog entries
- [x] Offline banner text/visibility polish
- [x] Tests: persistence round-trip, dedup, cap; widget test for the menu; full suite green

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `3d6a6a4`; test list: `tests/test_recent.py` and `tests/test_recent_widgets.py` (added there), plus `tests/test_ui_harness.py`.
