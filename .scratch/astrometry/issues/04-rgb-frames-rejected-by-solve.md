# 04: Solve rejects RGB stack frames ("must be a 2-D image, got shape (3, 1920, 1080)")

**Found during:** live guided test (project ngc869, 2026-11-22). Pressing **Solve** on the freshly stacked device stack failed with `SolveError: the frame data must be a 2-D image, got shape (3, 1920, 1080)` — Seestar stacks are RGB FITS (3, H, W), and `read_fits_bytes` / `_squeeze_2d` (`src/seestar_controller/astrometry/solve.py:179-183`) only accept 2-D. The annotate/export path already collapses RGB to luminance (`src/seestar_controller/astrometry/annotate.py:106-111`, "RGB (H, W, 3) -> luminance"), so the solve path should reuse that helper.

**What to build:** `read_fits_bytes` (or `prepare_project_frame`) must accept RGB/RGBA arrays — both `(3, H, W)` channel-first and `(H, W, 3)` channel-last — and convert to a 2-D luminance float array before solving (same conversion as `annotate._as_2d`). Acceptance: solving the RGB stack from ngc869 succeeds (WCS written, `solved: N stars`); mono 2-D frames still work unchanged; `(H, W, 3)` and `(3, H, W)` both handled. Tests: solve a synthetic RGB FITS (both layouts) -> 2-D data reaches solve-field; error only for truly non-image shapes (e.g. 4-D).

**Blocked by:** None (astrometry 01-03 done)

**Status:** implemented in ae099c6 (pending review)

- [x] RGB -> 2-D luminance conversion in the solve input path (shared with annotate)
- [x] Channel-first and channel-last layouts handled
- [x] Tests: RGB stack solves; mono unchanged; 4-D still errors
- [x] Full suite green

## Comments

Implemented on branch `wt/fix-ast-04` (worktree `fix-ast-04`), commit `ae099c6`:

- `astrometry/annotate.py`: `_gray` (the RGB -> luminance helper at :106) now handles both channel layouts — channel-first `(3|4, H, W)` via `mean(axis=0)` in addition to channel-last `(H, W, 3|4)` via `mean(axis=2)`.
- `astrometry/solve.py`: `_squeeze_2d` collapses a 3-D RGB/RGBA frame to a 2-D luminance float32 array by reusing `annotate._gray` (its `ExportError` re-raised as `SolveError`); non-image shapes (4-D, or 3-D without a 3/4 channel axis) still raise `SolveError` "must be a 2-D image". The conversion is applied on all three input paths (FITS bytes via `read_fits_bytes`, FITS path, `data=`) through the new `_as_2d_with_header`, which also drops the stale `NAXIS`/`NAXISn` cards when the raw frame was not 2-D (a stale `NAXIS=3` made every WCS built from the header 3-dimensional and broke the Gaia detection pass). `prepare_project_frame` inherits the fix via `read_fits_bytes`.
- Tests (`tests/test_astrometry_solve.py`, `tests/test_astrometry_store.py`): synthetic RGB/RGBA FITS in both layouts -> `read_fits_bytes` returns the 2-D channel-mean luminance; `solve_frame`/`solve_fits_file` solve an RGB stack in both layouts (Gaia pattern match recovers the WCS within tolerance); the fake `solve-field` gains `FAKE_SOLVE_REQUIRE_2D` (exits 4 unless its input FITS is 2-D), proving 2-D luminance reaches the subprocess; `solve_project_frame` solves a stored RGB stack (WCS written into the stored FITS + db record with `n_stars`); mono 2-D passes through unchanged; 4-D and non-RGB 3-D still raise `SolveError`. Note: the old `(4, 4, 4)` bad-shape assertion moved to a real 4-D array `(2, 3, 4, 4)` — a 4-sized leading axis is now (correctly) accepted as RGBA per the ticket's "accept RGB/RGBA" / "error only for truly non-image shapes (e.g. 4-D)".

Verification (worktree `~/.vibe/worktrees/seestar-controller-dec7d727fea1/fix-ast-04`, venv created with `uv venv --system-site-packages` + `uv sync` + `uv pip install pygobject pycairo` so gi is present): `uv run pytest -q -n auto` -> 2887 passed, 2 skipped (environmental: missing `reproject`, gui-launch guard); `uv tool run ruff check` -> all checks passed; `uv tool run ty check` -> 108 diagnostics, identical count on pristine main (pre-existing gi-stub noise), none in the touched files.
