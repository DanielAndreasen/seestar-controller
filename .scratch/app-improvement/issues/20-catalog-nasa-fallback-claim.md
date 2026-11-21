# 20: catalog spec promises a "NASA/SIMBAD fallback" resolver that does not exist

**What to build:** align the catalog spec with the implemented behavior: either drop the overpromised fallback phrase, or implement a NASA resolver fallback.

Evidence: `.scratch/catalog/spec.md` Implementation Decisions: "Name resolution: SIMBAD via astroquery (or direct SIMBAD HTTP), plus a NASA/SIMBAD fallback" vs `grep -rin nasa src/seestar_controller/catalog/` → no matches; `catalog/simbad.py` implements SIMBAD TAP only. Issue 01 (the ticket actually implemented) only requires SIMBAD, so behavior is as ticketed — the spec overpromises.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] Spec text no longer promises an unimplemented NASA fallback (or a fallback resolver is implemented and tested)
- [ ] No code changes if descoped; docs-only otherwise; full suite green if code changes

## Comments

Audit finding (kind: incorrect-spec-claim, severity: low).
