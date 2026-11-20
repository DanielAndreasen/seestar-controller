# 02: Report sections — frame quality + moving objects

**What to build:** add the frame-quality summary (median FWHM, % rejected, trend sparkline) and detected movers (from moving-objects) as report sections, so the report covers the whole night. Demoable: report on a session with scored frames + a detected mover includes both sections.

**Blocked by:** session-report 01, frame-quality 01 (ready-for-agent, not yet implemented), moving-objects 01 (not yet ticketed)

**Status:** ready-for-agent

- [ ] Quality section: pull session_quality (frame-quality 01) into the HTML report
- [ ] Movers section: pull moving-objects detections into the report
- [ ] Tests: synthetic data -> sections present; missing data -> sections gracefully omitted
- [ ] Full suite green

## Comments

(implementor fills in)
