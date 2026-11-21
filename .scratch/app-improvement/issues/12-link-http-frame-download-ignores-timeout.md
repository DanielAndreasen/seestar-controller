# 12: fw 9.75 HTTP frame download silently ignores the caller's `timeout`

**What to build:** honor the `timeout` argument on the HTTP album download path (the default on fw 9.75), so a hung HTTP GET blocks the capture worker for the configured timeout, not the hardcoded 30 s.

Evidence: `src/seestar_controller/link/frames.py:445-462` — `fetch_frame_bytes(..., timeout=...)` accepts timeout but the HTTP branch calls `fetcher(url)` without forwarding it; `default_fetch` (frames.py:295-297) uses `urlopen(url, timeout=30.0)`. Breaks the documented timeout contract of `download_frames(..., timeout=...)` and capture's `download_timeout`.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] The HTTP fetch path receives and uses the caller's timeout (e.g. `Callable[[str, float], bytes]` or `functools.partial`); default behavior unchanged when no timeout given
- [ ] Tests in `tests/test_frames.py` cover timeout forwarding on the HTTP path; full suite green; ty + ruff clean

## Comments

Audit finding (kind: bad-implementation, severity: low). Covered-by pre-audit: `.scratch/seestar-link/issues/06-frame-downloads.md`.
