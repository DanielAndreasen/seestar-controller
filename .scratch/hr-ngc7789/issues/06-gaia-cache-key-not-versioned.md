# 06: pre-fix Gaia disk-cache entries are served fresh for up to 7 days (no schema version in the key)

**What to build:** version the Gaia disk-cache key (or require the new error fields in the shape check) so cache entries written before issue 02 (9-field payloads, `parallax_error=None`) are not served as fresh for `DEFAULT_TTL_S` = 7 days (`src/seestar_controller/gaia/query.py:111`). Currently `_REQUIRED_STAR_FIELDS` (:583) excludes the error fields and `_is_gaia_rows` (:626-630) accepts 9-field payloads, `_row_to_star` (:604-623) reads errors via `.get` — a probe showed a 9-field entry returned by `query_gaia_cached` with no HTTP and `parallax_error=None`. Fix direction: include a format version in `gaia_cache_key` (bumping it invalidates old entries) or tighten `_is_gaia_rows` to require the 12 fields, keeping the stale-offline fallback working for entries that have them.

Evidence: hr-ngc7789 driver re-run had to manually delete the stale cache entry under `~/.config/seestar-controller/cache/gaia/` before the refetch produced error columns. Kind: bad-implementation (cache correctness), severity: medium.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] Old-format cache entries are either re-fetched or only served via the stale/offline path, never as fresh
- [ ] Stale-offline fallback still works for same-format entries
- [ ] Tests for both; full suite green; `ty`/`ruff` clean

## Comments

Found by the hr-ngc7789 driver re-run (branch wt/hr-ngc7789, commit a3c98ed).
