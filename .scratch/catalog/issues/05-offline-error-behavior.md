# 05: Offline and error behavior

**What to build:** graceful degradation — when the network is down, catalog calls return cached data if available and otherwise raise a clear, user-actionable error; malformed/timeout responses never crash the app. Demoable: network mocked as down → cached result returned; no cache → clean error.

**Blocked by:** 03, 04

**Status:** ready-for-agent

- [ ] Network failure with cached entry → cached result
- [ ] Network failure without cache → actionable error, no traceback to the user
- [ ] Timeout and malformed JSON handled the same way
- [ ] Tests: all failure modes with mocked transport
