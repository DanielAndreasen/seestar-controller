# 03: Frame storage

**What to build:** storing FITS data — sub-frames and stacks are written atomically (temp + rename) into `subs/<session>/` and `stacks/<session>/`, each with a metadata row (JD from header or mtime, exposure, gain, target). Demoable: store a synthetic FITS as a sub-frame, read back file + metadata.

**Blocked by:** 02

**Status:** ready-for-agent

- [ ] `store_frame(project, session_id, kind, fits_bytes, meta)` writes file + db row atomically
- [ ] JD extracted from FITS header DATE-OBS, fallback to file mtime
- [ ] Frames listed per session with metadata
- [ ] Tests: store/list round-trip; interrupted write leaves no partial file
