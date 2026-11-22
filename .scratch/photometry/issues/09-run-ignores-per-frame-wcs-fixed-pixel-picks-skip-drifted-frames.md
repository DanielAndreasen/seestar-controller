# Issue 09: photometry run uses fixed pixel picks — no per-frame WCS reprojection, so drifted/rotated frames are silently skipped (33 of 512 measured)

**Status:** completed (fixed in 14ce236 on main: per-frame WCS reprojection of sky picks in measure/run, fixed-pixel fallback, aggregated skip reasons + WCS coverage on RunReport and in the results view; reviewed APPROVE)

## Found while
Live session 2026-11-22, project `vw-cep`, session `20261122T192149035964Z` (512 subs over ~2 h on the alt-az S50). Picks were saved with BOTH pixel and sky positions (`StarPick.x/y` + `ra_deg/dec_deg`, target VW Cep + 2 comps). Run photometry produced only 33 measurements; the rest were skipped.

Probe (`measure_frame` on subs 0/1/5/10/30/60/100/200/300/400/511) shows nearly all skips are
`FrameSkippedError: non-positive flux ... at pick (x, y)` — i.e. the star is no longer inside the
aperture at the FIXED picked pixel. Over 2 h the alt-az field rotation + tracking drift moved the
stars many pixels, so a fixed pixel pick only works for the frames near the one the pick was made on.
`measure.py` never consults the per-frame WCS (`store.wcs.load_frame_wcs` / `frame_wcs` table), even
though the picks carry sky coordinates and 4 frames in the session are solved.

## Expected
- When a frame has a stored WCS (`seestar_controller.store.wcs.load_frame_wcs`) and a pick has
  `ra_deg`/`dec_deg`, reproject the sky position to that frame's pixel coordinates (WCS
  `world_to_pixel` / `sky_to_pixel` helper in `photometry.picking`) and measure there.
- Fall back to the fixed pixel pick only when the frame has no WCS or the pick has no sky coords
  (record which mode was used, or count WCS-reprojected vs fallback frames on the run report).
- The Run photometry tab should surface aggregated skip reasons (currently only count) and ideally
  offer batch-solving the session's unsolved subs so WCS reprojection applies to all frames.
- A frame whose reprojected pick still falls outside the image or yields non-positive flux stays a
  `SkippedFrame` — per-frame failures must still never kill the run.

## Scope
`src/seestar_controller/photometry/measure.py` (`_measure_frame_full` / `measure_frame_data` — add
an optional per-frame pixel resolution step), `src/seestar_controller/photometry/run.py` (pass the
frame's WCS through, aggregate skip reasons on `RunReport`), possibly
`src/seestar_controller/ui/photometry*` results view (show skip reasons + WCS coverage), tests in
`tests/test_photometry_*.py` (synthetic WCS frames where the star moves between frames).
