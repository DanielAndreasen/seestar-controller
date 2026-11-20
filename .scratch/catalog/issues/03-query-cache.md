# 03: Query cache

**What to build:** a JSON cache in the app config dir with configurable TTL that wraps every catalog query — repeat lookups are instant and work offline. Demoable: resolve the same name twice, second call served from cache with the network mocked as down.

**Blocked by:** 01

**Status:** implemented in c232b92

- [x] All catalog queries go through the cache wrapper
- [x] Cache files are JSON under the config dir, keyed by query
- [x] TTL respected; expired entries refetch
- [x] Tests: cache hit, miss, expiry, corrupted cache file falls back to network
