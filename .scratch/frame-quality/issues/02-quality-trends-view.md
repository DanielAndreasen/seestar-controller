# 02: Quality trends view (focus / sky / airmass per session)

**What to build:** per-session trend plots — FWHM vs time (focus drift), background vs time (sky quality/clouds), airmass vs time for the session target — plus a per-session quality summary card (median FWHM, % rejected). Demoable: open a session with scored frames; the three trend plots and summary render in the GUI.

**Blocked by:** frame-quality 01

**Status:** implemented in fbc5578 (merged)

- [x] Pure trend computation: airmass from target coords + frame times (astropy AltAz), series extraction from stored metrics
- [x] Widget: session quality view/section (reuse the analysis plotting widgets from period-analysis lightcurve view) rendering the three trends + summary
- [x] Wired into the app (Lightcurve view session selector or Observe session log — decide with existing patterns)
- [x] Widget tests via ui harness; full suite green

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `fbc5578`; test list: tests/test_frame_quality_trends.py, tests/test_frame_quality_widgets.py. Re-verified during the issue-17 audit: all referenced commits exist on main and the listed targeted tests pass (`uv run pytest <files> -q -n auto`).
