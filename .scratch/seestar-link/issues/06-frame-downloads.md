# 06: Frame downloads

**What to build:** saving data to disk — sub-frames and stacked FITS downloaded from the scope are written into a caller-given directory, each with a metadata sidecar (time, exposure, gain, target). Demoable: mock scope serves frames, client downloads them into a directory and the files are valid FITS with sidecars.

**Blocked by:** 04, 05

**Status:** implemented in 7f8bb25

- [x] Sub-frames and stacks are downloaded to the given directory
- [x] Each file has a metadata sidecar with time/exposure/gain/target
- [x] Files are written atomically (temp + rename)
- [x] Tests: download against mock scope, verify file contents and sidecars
