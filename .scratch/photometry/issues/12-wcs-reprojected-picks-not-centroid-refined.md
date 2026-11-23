# Issue 12: WCS-reprojected picks are not centroid-refined — ~5 px plate-solve pointing error puts the star core outside the r=4 aperture and contaminates the sky annulus, so nearly all frames skip with non-positive flux

**Status:** completed (fixed in fab4d09, merged to main: centroid refinement of WCS-reprojected picks before aperture photometry; reviewed APPROVE)

## Found while
Live re-test 2026-11-22 after issues 09+10 (project vw-cep, session 20261122T192149035964Z): all 512 subs now solved (frame_wcs 512/512), but the photometry re-run still measured only a handful (110 measurements total). Probe of `measure_frame` on subs 0/100/250/511: every frame skips `non-positive flux` at the REPROJECTED pick, e.g. frame 1 pick (750.3, 1309.8) with aperture sum 9194 vs sky-annulus background 3127.

Direct check against the FITS data: the true star (saturated, val ~65523 vs sky mean ~990) sits at local max ~(757, 1307) — only **4.5–6.7 px** from the WCS-predicted pixel on every frame probed. With aperture_radius=4 the aperture centered on the WCS pixel misses the star core, while the sky annulus (8–12 px) catches the star's wings → background overestimated → net flux negative → skip.

Control: snapping the target pick to the local max (pixel-only pick, no WCS) measures fine (frame 513 → mag OK).

## Expected
- After WCS reprojection (issue 09 path), refine each pick on the actual frame: e.g. windowed centroid (or brightest-pixel + centroid) within a small radius (~8–10 px) of the predicted pixel, before aperture photometry. Record that refinement happened (e.g. extend `FrameMeasurement.wcs_reprojected` semantics or add an offset field).
- Only skip when no significant local maximum is found near the predicted position (or flux still non-positive after refinement).
- Fixed-pixel fallback path (no WCS) keeps current behavior.
- Tests: synthetic frames with a star offset 5 px from the WCS-predicted pixel — must measure OK after refinement; no-star frames still skip with the right kind.

## Scope
`src/seestar_controller/photometry/measure.py` (`resolve_pick_pixels` / `measure_frame_data` — add the refinement step operating on the frame data), `run.py` (pass-through/report), tests in `tests/test_photometry_wcs_reprojection.py` (or a new test file).
