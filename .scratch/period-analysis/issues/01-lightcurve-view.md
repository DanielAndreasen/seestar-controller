# 01: Lightcurve view

**What to build:** the lightcurve plot in the app — mag vs JD for the current project, sessions colored differently, error bars per point, zoom/pan. Demoable: temp project with measurements across two sessions renders as one plot with distinct session colors.

**Blocked by:** ui-shell 01, project-store 05

**Status:** implemented in c0e0dcc

- [x] Lightcurve renders mag vs JD from project-store
- [x] Sessions visually distinguished (color per session)
- [x] Error bars shown per point
- [x] Zoom/pan works
- [x] Tests: view controller with temp project data; empty project handled
