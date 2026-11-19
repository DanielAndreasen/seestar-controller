# 02: Settings dialog

**What to build:** a settings dialog for scope IP, PEM key path, and project root, persisted to a JSON file in the app config dir and reloaded on start. Demoable: change settings, restart app, values persist.

**Blocked by:** 01

**Status:** ready-for-agent

- [ ] Settings dialog edits scope IP, key path, project root
- [ ] Settings persisted to config JSON and loaded on startup
- [ ] Invalid values (bad IP, missing dir) flagged in the dialog
- [ ] Tests: save/load round-trip, defaults when no config exists
