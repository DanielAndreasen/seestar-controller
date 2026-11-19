# 03: Device facade — state, goto, park

**What to build:** the high-level device facade on top of the transport: connect, query the scope's current state (mode, current target, stacking progress), slew to RA/Dec, and park. Events from the scope are surfaced through the facade as callbacks. Demoable: against the mock scope, connect → read state → goto → park.

**Blocked by:** 01

**Status:** implemented in 9b05bd5

- [x] `connect(ip)` returns a connected facade with live state
- [x] `state()` reflects the mock scope's mode/target/progress
- [x] `goto(ra, dec)` sends the slew and reports arrival via event or state
- [x] `park()` shuts the scope down
- [x] Facade-level tests run entirely against the mock scope
