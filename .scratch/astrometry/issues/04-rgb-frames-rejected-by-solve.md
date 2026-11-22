# 04: Solve rejects RGB stack frames ("must be a 2-D image, got shape (3, 1920, 1080)")

**Found during:** live guided test (project ngc869, 2026-11-22). Pressing **Solve** on the freshly stacked device stack failed with `SolveError: the frame data must be a 2-D image, got shape (3, 1920, 1080)` — Seestar stacks are RGB FITS (3, H, W), and `read_fits_bytes` / `_squeeze_2d` (`src/seestar_controller/astrometry/solve.py:179-183`) only accept 2-D. The annotate/export path already collapses RGB to luminance (`src/seestar_controller/astrometry/annotate.py:106-111`, "RGB (H, W, 3) -> luminance"), so the solve path should reuse that helper.

**What to build:** `read_fits_bytes` (or `prepare_project_frame`) must accept RGB/RGBA arrays — both `(3, H, W)` channel-first and `(H, W, 3)` channel-last — and convert to a 2-D luminance float array before solving (same conversion as `annotate._as_2d`). Acceptance: solving the RGB stack from ngc869 succeeds (WCS written, `solved: N stars`); mono 2-D frames still work unchanged; `(H, W, 3)` and `(3, H, W)` both handled. Tests: solve a synthetic RGB FITS (both layouts) -> 2-D data reaches solve-field; error only for truly non-image shapes (e.g. 4-D).

**Blocked by:** None (astrometry 01-03 done)

**Status:** open — found in live test 2026-11-22

- [ ] RGB -> 2-D luminance conversion in the solve input path (shared with annotate)
- [ ] Channel-first and channel-last layouts handled
- [ ] Tests: RGB stack solves; mono unchanged; 4-D still errors
- [ ] Full suite green
