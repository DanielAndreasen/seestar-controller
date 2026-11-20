# 05: Offline and error behavior

**What to build:** graceful degradation — when the network is down, catalog calls return cached data if available and otherwise raise a clear, user-actionable error; malformed/timeout responses never crash the app. Demoable: network mocked as down → cached result returned; no cache → clean error.

**Blocked by:** 03, 04

**Status:** implemented in 95145a0

- [x] Network failure with cached entry → cached result
- [x] Network failure without cache → actionable error, no traceback to the user
- [x] Timeout and malformed JSON handled the same way
- [x] Tests: all failure modes with mocked transport

## Comments

Implemented as `catalog/facade.py` — the single high-level entry point (`Catalog` / `lookup_name` / `lookup_standards`), composing the raw uncached queries with `QueryCache` (new `QueryCache.get_any` reads an entry ignoring the TTL without discarding it). Policy decision (documented in the module docstring): **stale-on-failure** — on ANY fetch failure (network down, timeout, HTTP error, malformed response, or unexpected error) a valid cached entry is served even if expired, because catalog data changes slowly; expired cache + network down → stale entry, not an error. With no usable cache entry: unavailable/timeout/unexpected → `CatalogUnavailableError` naming the query and suggesting retry + the offline cache location; malformed → the specific `*ResponseError` re-raised. Raw `URLError`/`TimeoutError` are caught and translated — never a raw traceback. Raw uncached functions untouched; cache keys shared with the `*_cached` variants. Tests: `tests/test_catalog_offline.py` (30 tests, HTTP boundaries monkeypatched to raise, tmp_path caches, no real network).
