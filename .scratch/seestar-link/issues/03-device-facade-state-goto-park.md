# 03: Device facade — state, goto, park

**What to build:** the high-level device facade on top of the transport: connect, query the scope's current state (mode, current target, stacking progress), slew to RA/Dec, and park. Events from the scope are surfaced through the facade as callbacks. Demoable: against the mock scope, connect → read state → goto → park.

**Blocked by:** 01

**Status:** ready-for-agent

- [ ] `connect(ip)` returns a connected facade with live state
- [ ] `state()` reflects the mock scope's mode/target/progress
- [ ] `goto(ra, dec)` sends the slew and reports arrival via event or state
- [ ] `park()` shuts the scope down
- [ ] Facade-level tests run entirely against the mock scope
