# 01: Lightcurve view

**What to build:** the lightcurve plot in the app — mag vs JD for the current project, sessions colored differently, error bars per point, zoom/pan. Demoable: temp project with measurements across two sessions renders as one plot with distinct session colors.

**Blocked by:** ui-shell 01, project-store 05

**Status:** implemented in 79013706b4b1793dd804547250163638c8ed108e

- [x] Lightcurve renders mag vs JD from project-store
- [x] Sessions visually distinguished (color per session)
- [x] Error bars shown per point
- [x] Zoom/pan works
- [x] Tests: view controller with temp project data; empty project handled
