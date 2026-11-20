# 03: Exclude flagged frames from photometry runs

**What to build:** photometry runs accept the session's flagged-frame set and exclude those frames from measurement, with the exclusion recorded on the measurements. Demoable: run photometry on a session; flagged frames are skipped; results view shows how many frames were excluded and why.

**Blocked by:** frame-quality 01, photometry 04 (done)

**Status:** ready-for-agent

- [ ] `RunController`/photometry run takes optional excluded-frame set (default: flagged frames of the session); excluded frames skipped in `photometry/run.py`, exclusion count surfaced in results
- [ ] Measurements record exclusion (flag/count), CSV export unchanged or extended consistently
- [ ] Tests: exclusion removes exactly the flagged frames from photometry input; results reflect it; full suite green

## Comments

(implementor fills in)
