# 12: fw 9.75 HTTP frame download silently ignores the caller's `timeout`

**What to build:** honor the `timeout` argument on the HTTP album download path (the default on fw 9.75), so a hung HTTP GET blocks the capture worker for the configured timeout, not the hardcoded 30 s.

Evidence: `src/seestar_controller/link/frames.py:445-462` — `fetch_frame_bytes(..., timeout=...)` accepts timeout but the HTTP branch calls `fetcher(url)` without forwarding it; `default_fetch` (frames.py:295-297) uses `urlopen(url, timeout=30.0)`. Breaks the documented timeout contract of `download_frames(..., timeout=...)` and capture's `download_timeout`.

**Blocked by:** None (can start immediately)

**Status:** implemented in e000de7 (pending review)

- [x] The HTTP fetch path receives and uses the caller's timeout (e.g. `Callable[[str, float], bytes]` or `functools.partial`); default behavior unchanged when no timeout given
- [x] Tests in `tests/test_frames.py` cover timeout forwarding on the HTTP path; full suite green; ty + ruff clean

## Comments

Audit finding (kind: bad-implementation, severity: low). Covered-by pre-audit: `.scratch/seestar-link/issues/06-frame-downloads.md`.

Implemented in `e000de7`: the injected fetcher contract is now `Callable[[str, float], bytes]`; `fetch_frame_bytes` resolves `timeout=None` to the new `frames.DEFAULT_FETCH_TIMEOUT` (30.0 — default behavior unchanged) and calls `fetcher(url, timeout)`, so the caller's timeout reaches `default_fetch` -> `urlopen` on the default fw 9.75 path and any injected fetch. `MockScope.fetch_http` accepts (and ignores) the timeout; `Device.download_frames` passes it through; `tests/test_compat.py`'s `broken_fetch` fake adapted to the new signature. New tests in `tests/test_frames.py` (7): spy-fetch forwarding at `fetch_frame_bytes`, default-fetch path spied at `urlopen`, 30 s default when no timeout given, end-to-end `download_frames` and facade `Device.download_frames` forwarding. Targeted tests: `uv run pytest tests/test_frames.py tests/test_compat.py tests/test_observing_capture.py -q` -> 86 passed. `uv tool run ty check` -> All checks passed! `uv tool run ruff check` -> All checks passed!
