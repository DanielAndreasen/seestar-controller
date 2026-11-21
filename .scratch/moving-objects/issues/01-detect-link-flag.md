# 01: Moving-object detection core — detect, link, flag linear movers (pure)

**What to build:** the pure (no-gi) core of moving-objects: detect sources per sub-frame of a session (photutils DAOStarFinder, low threshold), link detections across frames into tracks by nearest-neighbor in pixel coords (subs of a session share the same pointing; WCS optional — fall back to pixel space with a clear note), flag linear movers (>= N frames with consistent velocity via linear fit + residual cut), and report track coordinates/times + angular velocity + direction. Persist tracks per session in the project (file-based under the session dir, or store if schema-free — decide with existing patterns; do NOT bump the store schema). Demoable: synthetic subs with one injected linear mover + static stars -> mover track recovered with correct speed/direction, static stars not flagged.

**Blocked by:** None (project-store, photometry detection patterns done)

**Status:** ready-for-agent

- [ ] Pure `moving_objects/` package, no gi (subprocess boundary test); astropy/photutils lazy
- [ ] `detect_in_subs(project, session_id)` -> per-frame detections; `link_tracks(detections, max_motion_px)` -> tracks; `flag_linear_movers(tracks, min_frames)` -> mover reports (start/end pos+time, speed px/frame or arcsec/frame if WCS, direction, residual)
- [ ] Tracks persisted per session (file-based under session dir preferred; no schema bump)
- [ ] Tests: synthetic subs with injected linear mover -> track recovered, speed/direction correct, static stars not flagged; no-WCS fallback; persistence round-trip; no-gi boundary; no network
- [ ] Full suite green

## Comments

(implementor fills in)
