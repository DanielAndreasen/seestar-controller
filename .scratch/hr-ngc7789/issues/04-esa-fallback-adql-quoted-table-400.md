# 04: ESA fallback ADQL gets HTTP 400 — quoted dotted table name not resolved

**What to build:** fix the ESA-dialect ADQL in `build_cone_query` (`src/seestar_controller/gaia/query.py`, FROM clause ~:379, `ESA_GAIA_DR3_TABLE` :96, `_TAP_DIALECTS` :207-216): it emits `FROM "gaiadr3.gaia_source"` (single quoted dotted identifier) but the Gaia archive only resolves `gaiadr3.gaia_source` unquoted or `"gaiadr3"."gaia_source"`. Emit the table unquoted (or schema-quoted) for the ESA dialect so the fallback endpoint actually answers.

Evidence: `Gaia TAP at https://gea.esac.esa.int/tap-server/tap/sync returned HTTP 400 (400) for the query`, body `Cannot parse query ... 14 unresolved identifiers: gaiadr3.gaia_source ... - Unknown table ""gaiadr3.gaia_source"" !`. Repro: `build_cone_query(..., url=ESA_GAIA_TAP_SYNC_URL)` → `fetch_gaia_tap_json` → 400. Seen in the hr-ngc7789 driver run-log (attempt 1 of the cross-match after the issue-01 fallback merge).

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] ESA-dialect ADQL parses on the real archive (table reference unquoted or schema-quoted); VizieR dialect unchanged
- [ ] New test asserts the ESA query string form; existing `test_build_cone_query_esa_mirror_uses_the_gaia_archive_dialect` updated
- [ ] Full suite `uv run pytest -q -n auto` green; `ty`/`ruff` clean

## Comments

Found by the hr-ngc7789 driver re-run (branch wt/hr-ngc7789, commit a3c98ed, run-log.json attempt 1). Kind: bad-implementation, severity: high — the issue-01 fallback is useless against the real ESA mirror until this is fixed. Note: issue 05 (missing error columns in the ESA column list) is a separate ticket; fix both together if convenient but keep the commits separable.
