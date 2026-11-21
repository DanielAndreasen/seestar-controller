# 03: Recent projects list + offline indicator polish

**What to build:** the app remembers recently opened projects (persisted in the app config dir, e.g. `~/.config/seestar-controller/recent.json`, atomic write, capped list) and offers them in the open dialog / a "Recent projects" submenu for one-click reopening; the offline banner is clearly visible and states that no telescope is connected. Demoable: open a project offline, quit, relaunch — project appears in recent list, one click reopens it offline.

**Blocked by:** offline-analysis 01

**Status:** implemented in 3d6a6a4 (merged)

- [ ] Recent-projects persistence in app config dir (atomic, capped, most-recent-first, dedup)
- [ ] UI: recent projects menu/dialog entries
- [ ] Offline banner text/visibility polish
- [ ] Tests: persistence round-trip, dedup, cap; widget test for the menu; full suite green

## Comments

(implementor fills in)
