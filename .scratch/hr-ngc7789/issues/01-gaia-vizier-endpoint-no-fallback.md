# 01: Gaia cross-match hard-fails when the hardcoded VizieR TAP endpoint is down — no mirror fallback

**What to build:** make Gaia cone searches resilient: when the primary endpoint (`VIZIER_TAP_SYNC_URL`, `src/seestar_controller/gaia/query.py:69`, down since 2026-10-29 per the comment at :66-68) is unreachable, fall back to the ESA Gaia TAP mirror (`https://gea.esac.esa.int/tap-server/tap/sync`, verified HTTP 200) — and/or expose a `url=` parameter through `query_gaia` / `query_gaia_cached` (`fetch_gaia_tap_json` already accepts one) so callers/tests can point at a mirror. Keep the disk cache (`QueryCache`, key `gaia_cache_key`) semantics unchanged: a cached result must still win without any network call.

Evidence: driving `CrossmatchController.run_crossmatch()` headlessly on solved stack frame 3 of `/home/daniel/SeestarProjects/ngc7789` raised `GaiaUnavailableError: Gaia DR3 cone search around (ra=0.9653, dec=55.3454) radius=0.7765 deg failed and no cached result is available: could not reach VizieR Gaia TAP at https://tapvizier.cds.unistra.fr/TAPVizieR/TAP/sync: <urlopen error timed out>` — raised at `gaia/query.py:333` and `:600`, surfaced via `gaia/overlay.py:698-701`. Workaround used during the audit (no code change): seeded the disk cache from the ESA mirror using `build_cone_query` output with translated column names; the retry then succeeded cache-only (47/104 matched at r=1.5", G<20).

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] A cone search with the primary endpoint blocked/unreachable falls back to the ESA mirror (or an explicit `url=`) and returns rows
- [ ] Cache hit still returns without any network call
- [ ] Existing gaia tests pass; new test covers the fallback path (mock both endpoints)
- [ ] Full suite `uv run pytest -q -n auto` stays green; `uv tool run ty check` and `uv tool run ruff check` clean

## Comments

Found by the hr-ngc7789 driver subagent (branch wt/hr-ngc7789, run-log.json in docs/hr-ngc7789/artifacts/). Kind: bad-implementation / robustness, severity: high (the cross-match — and therefore the H-R diagram — is impossible while VizieR is down).
