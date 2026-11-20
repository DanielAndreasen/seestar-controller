# 02: Pointing error + drift statistics (pure)

**What to build:** per-session pointing accuracy and drift: compare the session's requested target RA/Dec (observing queue entry) against the solved WCS center -> pointing error; across the session's solved subs, compute RA/Dec offset vs. time -> drift series for a drift plot. Demoable: synthetic session with injected offsets -> pointing error and drift math correct.

**Blocked by:** astrometry 01, observing queue (done)

**Status:** ready-for-agent

- [ ] Pure functions: `pointing_error(project, session_id)` (requested vs solved center, arcsec) and `drift_series(project, session_id)` (offset vs frame time)
- [ ] Persisted/derived from stored WCS per frame (astrometry 01) + queue entry coords
- [ ] Tests: injected offsets -> correct error/drift within tolerance; missing WCS frames skipped with count reported
- [ ] Full suite green

## Comments

(implementor fills in)
