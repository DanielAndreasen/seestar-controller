# 02: Offline analysis views wired to stored project data

**What to build:** in offline mode, all analysis views operate on the stored project: session/frame/stack browser (projects-view) populated from the store; photometry view, astrometry solve button, gaia crossmatch overlay, mosaic stitch, image enhance, variable-scan view, H-R diagram view, period-analysis lightcurve view, and session report all runnable offline against stored frames. Demoable: open a project offline, run a photometry measurement, plate-solve a stack, stitch a mosaic, run a variable scan, generate a session report — all without any scope connection.

**Blocked by:** offline-analysis 01

**Status:** ready-for-agent

- [ ] Each analysis view works when the app is in offline mode (no live session required; controllers read from the store only)
- [ ] Solve/enhance/mosaic/scan/report actions write results back to the project as usual
- [ ] Widget/integration tests: synthetic project opened offline, each analysis action exercised with link mocked to fail; full suite green

## Comments

(implementor fills in)
