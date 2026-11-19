# 03: Period selection + phase fold

**What to build:** choosing a period and folding — click a periodogram peak or enter a period manually (days), then render the phase-folded lightcurve (phase 0–1) covering all sessions; the chosen period is saved in the project. Demoable: synthetic data → select peak → folded plot shows one clean cycle.

**Blocked by:** 01, 02, project-store 01

**Status:** ready-for-agent

- [ ] Clicking a periodogram peak selects that period; manual entry also possible
- [ ] Fold: phase = ((jd − jd0) / period) mod 1, jd0 default first observation
- [ ] Folded plot covers all sessions, phase 0–1
- [ ] Chosen period persisted in project.json
- [ ] Tests: fold math on known jd/period; period save/load round-trip
