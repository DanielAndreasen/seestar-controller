# 02: Offline analysis views wired to stored project data

**What to build:** in offline mode, all analysis views operate on the stored project: session/frame/stack browser (projects-view) populated from the store; photometry view, astrometry solve button, gaia crossmatch overlay, mosaic stitch, image enhance, variable-scan view, H-R diagram view, period-analysis lightcurve view, and session report all runnable offline against stored frames. Demoable: open a project offline, run a photometry measurement, plate-solve a stack, stitch a mosaic, run a variable scan, generate a session report — all without any scope connection.

**Blocked by:** offline-analysis 01

**Status:** implemented in f81651e (merged)

- [x] Each analysis view works when the app is in offline mode (no live session required; controllers read from the store only)
- [x] Solve/enhance/mosaic/scan/report actions write results back to the project as usual
- [x] Widget/integration tests: synthetic project opened offline, each analysis action exercised with link mocked to fail; full suite green

## Comments

Retroactive tick (app-improvement issue 17, docs-only): the Status line above already records the merge, so the acceptance boxes are checked to match it. Implementing commit `f81651e`; test list: `tests/test_offline_analysis.py` and `tests/test_offline_analysis_widgets.py` (added there).
