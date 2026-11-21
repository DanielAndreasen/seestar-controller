# 02: Report sections — frame quality + moving objects

**What to build:** add the frame-quality summary (median FWHM, % rejected, trend sparkline) and detected movers (from moving-objects) as report sections, so the report covers the whole night. Demoable: report on a session with scored frames + a detected mover includes both sections.

**Blocked by:** session-report 01, frame-quality 01 (ready-for-agent, not yet implemented), moving-objects 01 (not yet ticketed)

**Status:** implemented in e7a1925 (merged)

- [x] Quality section: pull session_quality (frame-quality 01) into the HTML report
- [x] Movers section: pull moving-objects detections into the report
- [x] Tests: synthetic data -> sections present; missing data -> sections gracefully omitted
- [x] Full suite green

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `e7a1925`; test list: `tests/test_report_sections.py` (added there), with `tests/test_report.py` and `tests/test_report_boundary.py` extended.
