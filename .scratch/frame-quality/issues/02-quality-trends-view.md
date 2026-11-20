# 02: Quality trends view (focus / sky / airmass per session)

**What to build:** per-session trend plots — FWHM vs time (focus drift), background vs time (sky quality/clouds), airmass vs time for the session target — plus a per-session quality summary card (median FWHM, % rejected). Demoable: open a session with scored frames; the three trend plots and summary render in the GUI.

**Blocked by:** frame-quality 01

**Status:** ready-for-agent

- [ ] Pure trend computation: airmass from target coords + frame times (astropy AltAz), series extraction from stored metrics
- [ ] Widget: session quality view/section (reuse the analysis plotting widgets from period-analysis lightcurve view) rendering the three trends + summary
- [ ] Wired into the app (Lightcurve view session selector or Observe session log — decide with existing patterns)
- [ ] Widget tests via ui harness; full suite green

## Comments

(implementor fills in)
