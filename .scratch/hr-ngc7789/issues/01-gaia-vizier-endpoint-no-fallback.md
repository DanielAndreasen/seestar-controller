# 01: Gaia cross-match hard-fails when the hardcoded VizieR TAP endpoint is down — no mirror fallback

**What to build:** make Gaia cone searches resilient: when the primary endpoint (`VIZIER_TAP_SYNC_URL`, `src/seestar_controller/gaia/query.py:69`, down since 2026-10-29 per the comment at :66-68) is unreachable, fall back to the ESA Gaia TAP mirror (`https://gea.esac.esa.int/tap-server/tap/sync`, verified HTTP 200) — and/or expose a `url=` parameter through `query_gaia` / `query_gaia_cached` (`fetch_gaia_tap_json` already accepts one) so callers/tests can point at a mirror. Keep the disk cache (`QueryCache`, key `gaia_cache_key`) semantics unchanged: a cached result must still win without any network call.

Evidence: driving `CrossmatchController.run_crossmatch()` headlessly on solved stack frame 3 of `/home/daniel/SeestarProjects/ngc7789` raised `GaiaUnavailableError: Gaia DR3 cone search around (ra=0.9653, dec=55.3454) radius=0.7765 deg failed and no cached result is available: could not reach VizieR Gaia TAP at https://tapvizier.cds.unistra.fr/TAPVizieR/TAP/sync: <urlopen error timed out>` — raised at `gaia/query.py:333` and `:600`, surfaced via `gaia/overlay.py:698-701`. Workaround used during the audit (no code change): seeded the disk cache from the ESA mirror using `build_cone_query` output with translated column names; the retry then succeeded cache-only (47/104 matched at r=1.5", G<20).

**Blocked by:** None (can start immediately)

**Status:** completed — reviewed (approved) and merged to main in 81af987

- [x] A cone search with the primary endpoint blocked/unreachable falls back to the ESA mirror (or an explicit `url=`) and returns rows
- [x] Cache hit still returns without any network call
- [x] Existing gaia tests pass; new test covers the fallback path (mock both endpoints)
- [x] Full suite `uv run pytest -q -n auto` stays green; `uv tool run ty check` and `uv tool run ruff check` clean

## Comments

Found by the hr-ngc7789 driver subagent (branch wt/hr-ngc7789, run-log.json in docs/hr-ngc7789/artifacts/). Kind: bad-implementation / robustness, severity: high (the cross-match — and therefore the H-R diagram — is impossible while VizieR is down).

Implemented in `8572e8a` on branch `wt/hr-ngc7789-01` (worktree hr-ngc7789-01). What changed, all in `src/seestar_controller/gaia/query.py` (+ re-exports in `gaia/__init__.py`, + tests in `tests/test_gaia_query.py`):

- New endpoint chain `GAIA_TAP_SYNC_URLS = (VIZIER_TAP_SYNC_URL, ESA_GAIA_TAP_SYNC_URL)` with `ESA_GAIA_TAP_SYNC_URL = "https://gea.esac.esa.int/tap-server/tap/sync"` (the ESA Gaia archive mirror, verified HTTP 200). `query_gaia` / `query_gaia_cached` walk it via the new `_cone_search` helper (same pattern as `catalog/simbad.py`'s `SIMBAD_TAP_SYNC_URLS`): first endpoint that answers wins; only when every endpoint is unreachable (network/HTTP → `GaiaUnavailableError`) is one aggregated `GaiaUnavailableError` raised naming all endpoints and their errors. A malformed response from an endpoint that did answer still raises `GaiaResponseError` immediately (no fallback — that is a response problem, not an unreachable endpoint).
- Per-endpoint ADQL dialect: `build_cone_query(..., url=...)` now emits the VizieR query (`I/355/gaiadr3`, `RAJ2000`/`Gmag`/…) or the ESA-archive query (`gaiadr3.gaia_source` = `ESA_GAIA_DR3_TABLE`, `ra`/`phot_g_mean_mag`/… = `ESA_GAIA_COLUMNS`) per `_TAP_DIALECTS`; unknown URLs get the VizieR dialect. Response parsing already accepted both spellings via `_COLUMN_ALIASES`, so ESA responses parse unchanged.
- New `url=` parameter on `query_gaia` / `query_gaia_cached` (`fetch_gaia_tap_json` already had one) pins a single endpoint with no fallback, so callers/tests can point at any mirror.
- Disk cache semantics unchanged: key is still the rounded center + radius (+ g_limit + maxrec) via `gaia_cache_key`; a fresh cache entry is served before any endpoint is contacted (no network at all), a fallback success is cached like any other result, and the stale-entry offline fallback still applies when all endpoints fail.
- `fetch_gaia_tap_json` remains the single HTTP boundary; its error messages are now endpoint-neutral (they name the `url`).

Verification: `uv tool run ty check` and `uv tool run ruff check` — All checks passed. `uv run pytest -q tests/test_gaia_query.py test_gaia_boundary/gaia_pipeline/gaia_overlay/gaia_crossmatch` — 70 passed (8 new fallback tests: VizieR→ESA fallback order + per-endpoint ADQL + archive-spelled response parsing, aggregated all-down error, pinned `url=` with no fallback, no fallback on `GaiaResponseError`, cached fallback then cache hit with zero network calls, cached all-down error, ESA dialect in `build_cone_query`). Full suite `uv run pytest -q -n auto` — 2645 passed, 20 skipped, 1 failed: `test_visibility_planner.py::test_every_visibility_module_is_pure_or_widget_and_the_public_api_is_exported` (ModuleNotFoundError: gi), one of the two known pre-existing no-gi failures documented in docs/wiki/dependency-upgrade.md — reproduced identically on pristine main (e3d8373) in a no-gi venv (this worktree's plain `uv sync` venv has no PyGObject), so it is environmental, not caused by this change. Also sanity-checked the real `urlopen` path with a stub: VizieR timeout → ESA POST with archive-dialect ADQL → rows parsed; all-down → aggregated error listing both endpoints.
