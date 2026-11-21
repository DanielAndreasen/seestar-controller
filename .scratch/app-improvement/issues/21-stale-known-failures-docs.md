# 21: "known pre-existing failures" notes are stale now that the venv has a working gi

**What to build:** update the failure-list documentation so it matches both environments (no-gi sandbox and gi-present desktop/venv).

Evidence: `.scratch/dependency-upgrade/spec.md` lists "Known pre-existing failures in gi/headless sandboxes (test_observing_widgets x3, test_report_widgets x3, test_report_sections movers x1, test_moving_objects_widgets x5, test_visibility_planner purity)" and `AGENTS.md` + `docs/wiki/dependency-upgrade.md` say "two known pre-existing failures remain" — true only in the no-gi sandbox where widget suites skip. In this venv (gi 3.58.1 importable after dependency-upgrade issue 06, DISPLAY present) the full suite is `13 failed, 2808 passed, 2 skipped`: test_observing_widgets ×2, test_report_widgets ×3, test_report_sections movers ×1, test_moving_objects_widgets ×5, test_sub_frames_analysis_widgets ×1, test_offline_analysis_widgets ×1; the visibility-planner purity test passes. The actual failure set differs from the documented one, and the root causes (issues 01-04, 09-11) are now known.

**Blocked by:** None (can start immediately); should be updated again after issues 01-04 and 09-11 land

**Status:** ready-for-agent

- [ ] `docs/wiki/dependency-upgrade.md`, `.scratch/dependency-upgrade/spec.md`, and the AGENTS.md note state both environments explicitly and reference `.scratch/app-improvement/` issues 01-04/09-11 as the known failure root causes
- [ ] Docs-only change

## Comments

Audit finding (kind: incorrect-spec-claim, severity: medium — triage reference is wrong for gi-present environments).
