# 03: Query cache

**What to build:** a JSON cache in the app config dir with configurable TTL that wraps every catalog query — repeat lookups are instant and work offline. Demoable: resolve the same name twice, second call served from cache with the network mocked as down.

**Blocked by:** 01

**Status:** ready-for-agent

- [ ] All catalog queries go through the cache wrapper
- [ ] Cache files are JSON under the config dir, keyed by query
- [ ] TTL respected; expired entries refetch
- [ ] Tests: cache hit, miss, expiry, corrupted cache file falls back to network
