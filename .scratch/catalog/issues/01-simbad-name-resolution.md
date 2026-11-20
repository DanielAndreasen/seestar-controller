# 01: SIMBAD name resolution

**What to build:** resolve object names to J2000 coordinates via SIMBAD — `resolve_name("M42")` returns a list of candidates with coordinates and magnitude, and unknown/ambiguous names produce clear, actionable errors. Demoable: CLI or test resolves real names against mocked SIMBAD responses.

**Blocked by:** seestar-link 01 (uv project scaffold)

**Status:** implemented in 24590f2

- [x] Name → candidate list (name, RA/Dec J2000, magnitude) via SIMBAD
- [x] Unknown name returns a clear "not found" error
- [x] Ambiguous name returns multiple candidates for the caller to choose from
- [x] Tests with mocked SIMBAD HTTP responses cover found / not-found / ambiguous
