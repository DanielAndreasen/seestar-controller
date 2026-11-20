# 07: Resilience — reconnect, events, protocol log

**What to build:** production hardening of the link — exponential-backoff reconnect when the connection drops, a unified event/error dispatch (connection lost, scope error, stack finished), and an on-disk protocol log (all JSON-RPC traffic) for debugging the reverse-engineered protocol. Demoable: kill the mock scope mid-session, client reconnects automatically and logs the gap.

**Blocked by:** 01

**Status:** implemented in 7239c42

- [x] Dropped connection triggers reconnect with exponential backoff
- [x] Reconnect re-runs the handshake and restores subscriptions
- [x] Connection/scope events are dispatched to registered handlers exactly once
- [x] Protocol log writes all traffic to a rotating log file, toggleable
- [x] Tests: mock scope drops connection, client reconnects; event dispatch verified
