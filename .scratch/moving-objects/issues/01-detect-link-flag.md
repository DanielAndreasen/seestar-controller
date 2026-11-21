# 01: Moving-object detection core — detect, link, flag linear movers (pure)

**What to build:** the pure (no-gi) core of moving-objects: detect sources per sub-frame of a session (photutils DAOStarFinder, low threshold), link detections across frames into tracks by nearest-neighbor in pixel coords (subs of a session share the same pointing; WCS optional — fall back to pixel space with a clear note), flag linear movers (>= N frames with consistent velocity via linear fit + residual cut), and report track coordinates/times + angular velocity + direction. Persist tracks per session in the project (file-based under the session dir, or store if schema-free — decide with existing patterns; do NOT bump the store schema). Demoable: synthetic subs with one injected linear mover + static stars -> mover track recovered with correct speed/direction, static stars not flagged.

**Blocked by:** None (project-store, photometry detection patterns done)

**Status:** done

- [x] Pure `moving_objects/` package, no gi (subprocess boundary test); astropy/photutils lazy
- [x] `detect_in_subs(project, session_id)` -> per-frame detections; `link_tracks(detections, max_motion_px)` -> tracks; `flag_linear_movers(tracks, min_frames)` -> mover reports (start/end pos+time, speed px/frame or arcsec/frame if WCS, direction, residual)
- [x] Tracks persisted per session (file-based under session dir preferred; no schema bump)
- [x] Tests: synthetic subs with injected linear mover -> track recovered, speed/direction correct, static stars not flagged; no-WCS fallback; persistence round-trip; no-gi boundary; no network
- [x] Full suite green

## Comments

Implemented as `src/seestar_controller/moving_objects/` (modules: `errors`, `detect`, `link`, `flag`, `persist`), pure/no-gi with a subprocess boundary test; numpy/astropy/photutils imported lazily inside functions.

- `detect_in_subs(project, session_id, *, threshold=3.0, fwhm=4.0, max_frames=None)` — DAOStarFinder at a LOW threshold (3 sigma over the sigma-clipped background; movers are faint) per sub, ordered by jd; WCS read per sub header when present (blank CTYPE -> pixel-space fallback, `has_wcs=False`, `ra_deg/dec_deg=None`).
- `link_tracks(detections, max_motion_px=10.0)` — greedy nearest-neighbor in PIXEL coords (subs share pointing; WCS only used later for arcsec/frame), closest pairs first, each track/detection used once per frame; accepts Detection and/or FrameDetections.
- `flag_linear_movers(tracks, *, min_frames=5, max_residual_px=1.0, min_speed_px_per_frame=0.5)` — closed-form least-squares line of (x,y) vs frame index; flagged when n>=min_frames AND RMS residual<=max_residual_px AND fitted speed>=min_speed (the speed cut is what keeps static stars unflagged). Report: start/end pos+time, motion, speed px/frame (fit slope), direction deg (pixel space, +x=col/+y=row), fit residual, and speed arcsec/frame + start/end ra/dec when every point has a WCS position (else None).
- Persistence: FILE-BASED sidecar `<project>/subs/<session_id>/movers.json` (schema_version 1), atomic write (temp+fsync+os.replace, store_frame pattern); `save_tracks`/`load_tracks`/`tracks_path`; store schema untouched. Unknown session -> SessionNotFoundError; missing/corrupt file or unsafe session_id -> PersistenceError.
- Tests (`tests/test_moving_objects.py`, 60 tests): boundary (subprocess + source scan + module set + __all__), link/flag unit tests (gate split, curved track rejected, short track, static stars, arcsec speed, validation), and e2e over a tmp_path project: 8 synthetic 64x64 subs (3 static stars + one mover at 2 px/frame along +x) -> exactly 1 mover recovered with speed ~2.0 px/frame, direction ~0 deg, residual < 0.5, static stars not flagged; same with a 0.5 arcsec/px TAN WCS -> speed ~1.0 arcsec/frame; persistence round-trip + corrupt/missing/unsafe cases. No network.

