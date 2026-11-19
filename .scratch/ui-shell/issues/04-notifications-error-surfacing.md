# 04: Notifications + error surfacing

**What to build:** non-modal notifications — toasts for connection lost, scope errors, and stack events arriving from the link, so the user is informed without modal dialogs. Demoable: mock scope drops the connection, a toast appears.

**Blocked by:** 01, 03

**Status:** ready-for-agent

- [ ] Link events/errors surface as toast notifications
- [ ] No modal dialogs for background events
- [ ] Toasts dismiss automatically and can be reviewed in a log
- [ ] Tests: event → toast dispatch via mock facade
