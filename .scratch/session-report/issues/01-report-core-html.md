# 01: Session report core — self-contained HTML (pure)

**What to build:** the pure (no-gi) report generator: from a project + session id, produce a single self-contained HTML file (embedded base64 PNGs) containing the enhanced stack thumbnail, the session log, the lightcurve with the current period/fold overlay, and save it into the project (reports/ dir + DB record). Regenerating overwrites. Demoable: temp project with one synthetic session -> HTML contains all sections with embedded images; regenerate after adding data -> updated.

**Blocked by:** None for this slice (project-store, observing, photometry, period-analysis, image-enhance all done). Quality/movers sections are slice 02.

**Status:** ready-for-agent

- [ ] Pure `report/` package, no gi (subprocess boundary test); matplotlib Agg for plots, PIL for thumbnails, base64-embedded single HTML
- [ ] `generate_report(project, session_id) -> path`; sections: stack thumbnail, session log table, lightcurve + fold (period-analysis), measurements summary
- [ ] Saved into the project (reports/<session>.html + DB record per project-store patterns); regenerate overwrites
- [ ] Tests: synthetic session -> HTML contains expected sections + embedded images; regeneration; no-gi boundary; no network
- [ ] Full suite green

## Comments

(implementor fills in)
