# 13: `timeout or self.request_timeout` treats an explicit `timeout=0` as "use the default"

**What to build:** make the transport honor an explicit numeric timeout, including 0, instead of falling back to the default for any falsy value.

Evidence: `src/seestar_controller/link/transport.py:367,374` — `future.result(timeout=timeout or self.request_timeout)`; `timeout=0` is falsy, so a caller asking for an immediate reply gets the 10 s default instead of an instant `RequestTimeoutError`.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] `timeout=0` raises `RequestTimeoutError` immediately; `timeout=None` keeps the default; positive values unchanged (add tests)
- [ ] `uv run pytest tests/test_protocol.py tests/test_transport.py -q` (whichever exist) pass; full suite green; ty + ruff clean

## Comments

Audit finding (kind: bad-implementation, severity: low). Fix: `self.request_timeout if timeout is None else timeout`.
