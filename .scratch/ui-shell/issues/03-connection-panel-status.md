# 03: Connection panel + status

**What to build:** the connection UI — connect button in the header bar with status states (disconnected / connecting / connected / error), wired to the device facade from seestar-link. Demoable: against the mock scope, pressing connect shows connected and the scope state.

**Blocked by:** 01, 02, seestar-link 03 (device facade)

**Status:** ready-for-agent

- [ ] Connect button uses the scope IP from settings
- [ ] Status reflects disconnected/connecting/connected/error
- [ ] Successful connect shows the scope's current state
- [ ] Connection failure shows error state, not a crash
- [ ] Tests: controller state machine driven by a mock facade
