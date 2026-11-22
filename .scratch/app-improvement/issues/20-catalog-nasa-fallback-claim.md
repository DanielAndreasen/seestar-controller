# 20: catalog spec promises a "NASA/SIMBAD fallback" resolver that does not exist

**What to build:** align the catalog spec with the implemented behavior: either drop the overpromised fallback phrase, or implement a NASA resolver fallback.

Evidence: `.scratch/catalog/spec.md` Implementation Decisions: "Name resolution: SIMBAD via astroquery (or direct SIMBAD HTTP), plus a NASA/SIMBAD fallback" vs `grep -rin nasa src/seestar_controller/catalog/` → no matches; `catalog/simbad.py` implements SIMBAD TAP only. Issue 01 (the ticket actually implemented) only requires SIMBAD, so behavior is as ticketed — the spec overpromises.

**Blocked by:** None (can start immediately)

**Status:** implemented in 6da9b91 (pending review)

- [x] Spec text no longer promises an unimplemented NASA fallback (or a fallback resolver is implemented and tested)
- [x] No code changes if descoped; docs-only otherwise; full suite green if code changes

## Comments

Audit finding (kind: incorrect-spec-claim, severity: low).

Descoped to a docs-only fix in 6da9b91: `.scratch/catalog/spec.md` no longer mentions a NASA resolver — the Solution now says "SIMBAD" and the Implementation Decisions now read "SIMBAD via direct TAP HTTP (CDS Strasbourg first, then the official mirrors as fallback)", matching `src/seestar_controller/catalog/simbad.py` (SIMBAD TAP sync only, official-mirror fallback). `grep -rin nasa .scratch/catalog/` → no matches. No code changes, so no full-suite run needed; targeted catalog tests green: `uv run pytest tests/test_catalog_names.py tests/test_catalog_coords.py tests/test_catalog_cache.py tests/test_catalog_offline.py tests/test_catalog_standards.py -q` → 257 passed. `uv tool run ty check` → All checks passed! `uv tool run ruff check` → All checks passed!
