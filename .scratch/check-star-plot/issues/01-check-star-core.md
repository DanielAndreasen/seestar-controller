# 01: Check-star role in the photometry core (pure)

**What to build:** extend the photometry core with a check-star role: `StarPickSpec` role gains `check`; the check star is EXCLUDED from the comp ensemble (ensemble = mean of comps only); per frame, compute the check star's differential magnitude + error against the comp ensemble exactly like the target (same error propagation), and the target-minus-check curve. Persist the check-star choice in the project (store.stars). Demoable: synthetic frames where a cloud dims everything -> target and check move together, target-minus-check stays flat within errors; check excluded from ensemble math.

**Blocked by:** None (photometry 01-04, project-store 04 done)

**Status:** implemented in cad0b2e (merged)

- [ ] Pure change in photometry (picking/measure/run), no gi; role vocabulary extended in store/stars.py (ROLE_CHECK) with migration-free storage (picks table role column)
- [ ] `measure_frame_data` / run report expose per-frame check differential mag + err and target-minus-check; ensemble math provably excludes the check
- [ ] Check-star choice persisted per project (store.stars save/load round-trip)
- [ ] Tests: synthetic cloud-dimming frames -> target & check co-move, target-minus-check flat within errors; ensemble excludes check; persistence round-trip; no-gi boundary
- [ ] Full suite green

## Comments

(implementor fills in)
