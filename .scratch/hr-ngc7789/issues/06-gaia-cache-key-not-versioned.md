# 06: pre-fix Gaia disk-cache entries are served fresh for up to 7 days (no schema version in the key)

**What to build:** version the Gaia disk-cache key (or require the new error fields in the shape check) so cache entries written before issue 02 (9-field payloads, `parallax_error=None`) are not served as fresh for `DEFAULT_TTL_S` = 7 days (`src/seestar_controller/gaia/query.py:111`). Currently `_REQUIRED_STAR_FIELDS` (:583) excludes the error fields and `_is_gaia_rows` (:626-630) accepts 9-field payloads, `_row_to_star` (:604-623) reads errors via `.get` — a probe showed a 9-field entry returned by `query_gaia_cached` with no HTTP and `parallax_error=None`. Fix direction: include a format version in `gaia_cache_key` (bumping it invalidates old entries) or tighten `_is_gaia_rows` to require the 12 fields, keeping the stale-offline fallback working for entries that have them.

Evidence: hr-ngc7789 driver re-run had to manually delete the stale cache entry under `~/.config/seestar-controller/cache/gaia/` before the refetch produced error columns. Kind: bad-implementation (cache correctness), severity: medium.

**Blocked by:** None (can start immediately)

**Status:** implemented in 611ad0d (pending review)

- [x] Old-format cache entries are either re-fetched or only served via the stale/offline path, never as fresh
- [x] Stale-offline fallback still works for same-format entries
- [x] Tests for both; full suite green; `ty`/`ruff` clean

## Comments

Found by the hr-ngc7789 driver re-run (branch wt/hr-ngc7789, commit a3c98ed).

Implemented on branch `wt/hr-ngc7789-06` (worktree `hr-ngc7789-06`), commit `611ad0d`:

- `gaia/query.py`: new `CACHE_FORMAT_VERSION = 2` is embedded in every `gaia_cache_key()` JSON (`"v"` field), so pre-issue-02 (v1) keys hash to different cache files — those entries miss the lookup entirely and are never served, fresh or stale. `_is_gaia_rows` now requires `set(row) == _STAR_FIELDS` (all 12 fields, the same exact-set pattern as `catalog/standards.py`), so a 9-field payload under the current key is also rejected; `_REQUIRED_STAR_FIELDS` is removed and `_row_to_star` reads the error fields directly (the shape check guarantees them). Docstrings updated (module, `gaia_cache_key`, `query_gaia_cached`).
- Stale-offline fallback unchanged for current-format entries: an expired 12-field entry is still served when the fetch fails; a rejected v1 entry under the current key is refetched and overwritten on the next successful fetch (v1-key files are orphaned, never read).
- Tests (`tests/test_gaia_query.py`): key payload asserts the `"v"` field and that a legacy v1 key differs; new tests prove a v1-key entry is neither served fresh nor as a stale fallback, `_is_gaia_rows` rejects 9-field / extra-key / non-list payloads, and a 9-field entry under the current key is refetched (errors populated) and overwritten with 12-field rows; the existing stale-fallback test now also asserts the error columns survive the offline path.

Verification (worktree `~/.vibe/worktrees/seestar-controller-dec7d727fea1/hr-ngc7789-06`, venv recreated per the pyproject `--system-site-packages` + `pygobject`/`pycairo` recipe so gi is present): `uv run pytest -q -n auto` → 2879 passed, 2 skipped (environmental: missing `reproject`, gui-launch guard); `uv tool run ruff check` → all checks passed; `uv tool run ty check` → 108 repo-wide diagnostics, identical count on pristine main, none in the touched files (pre-existing gi-stub noise). Without gi in the venv the suite shows the single documented no-gi failure (`test_visibility_planner.py::test_every_visibility_module_is_pure_or_widget_and_the_public_api_is_exported`), which passes on main's gi-enabled venv.
