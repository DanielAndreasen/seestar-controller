# 02: MPC lookup for movers + annotated overlay view

**What to build:** (a) optional MPC query for each detected mover (catalog module HTTP+cache pattern, mocked in tests) matching by sky track/speed, persisted alongside movers.json; (b) the GUI: annotated overlay/blink on session frames showing mover tracks (reuse the analysis/enhance viewer patterns). Demoable: synthetic session with a mover -> MPC lookup mocked returns a known-object match recorded in the project; overlay view renders the track on the frame.

**Blocked by:** moving-objects 01

**Status:** ready-for-agent

- [x] Pure MPC lookup in moving_objects/ (catalog HTTP+cache pattern, no network in tests — mocked); match result persisted in movers.json sidecar (schema_version bump within the file, no store schema change)
- [x] Widget: overlay/blink view rendering mover tracks on session frames (gi-at-top, PEP 562 lazy export, ty-override + audit lists); wired into the app (session/results view — follow existing patterns)
- [x] Widget tests via ui harness; full suite green

## Comments

Implemented on `wt/moving-objects-02-mpc-overlay`:

- **Pure MPC lookup** (`moving_objects/mpc.py`): single HTTP boundary
  `fetch_mpc_checker_json` (urllib POST to the MPC Checker, JSON response);
  tests monkeypatch exactly that function — no network. Disk cache reuses
  `catalog.cache.QueryCache` under `<config>/cache/mpc/` (env override
  `SEESTAR_MPC_CACHE_DIR`), keyed by the rounded track params (pos 4 dec,
  rate 2 dec, PA 1 dec, JD 5 dec) + obscode, TTL 7 d; fresh entries are served
  with no HTTP, parse-before-cache + shape-check on read, stale-but-valid
  fallback on fetch failure, `MpcUnavailableError` with an empty cache.
  `mover_track_params` derives the sky track (mid position, arcsec/min rate,
  PA) from a `MoverReport` (raises `MpcLookupError` for pixel-only movers);
  `match_mover` is the pure matcher (offset <= 30 arcsec, relative rate diff
  <= 25%, PA diff <= 15 deg, best by offset). `lookup_movers` maps movers to
  matches (pixel-only -> None), `with_matches` attaches them, and
  `lookup_and_persist` writes them back into the sidecar.
- **Persistence**: `movers.json` sidecar bumped to `schema_version` 2 with
  `mpc_match` on each mover entry; v1 files are accepted and migrated on read
  (`mpc_match=None`). No store schema change.
- **Overlay** (`moving_objects/overlay.py`, pure): `tonemap_gray` +
  `render_frame_overlay` (PIL RGB, downscale to 1024 px, track polylines,
  mover color + label incl. the MPC designation, current-frame rings) and
  `MoverController` (project/viewer-follow, sessions, select/analyze
  (detect->link->flag->persist), cached `lookup_mpc` -> persist, frame blink
  navigation, frozen-snapshot listeners, store calls marshalled to the
  db-owning thread, errors recorded in `snapshot.error`).
- **Widget** (`moving_objects/overlay_view.py`): `MoverOverlayView` — gi at
  module top, re-exported lazily via PEP 562 `_WIDGET_EXPORTS`; session
  DropDown + Refresh + Detect movers + MPC lookup buttons (worker threads,
  renders marshalled with `GLib.idle_add`), movers ListBox, annotated frame
  `Gtk.Picture` (PIL -> PNG -> `Gdk.Texture`), Prev/Blink/Next controls,
  status label. Added to the pyproject ty-override list; wired into the app:
  `SeestarApplication` owns the shared `mover_controller` (following the
  shared viewer controller), `SeestarWindow` passes it to
  `build_view("photometry", mover_controller=...)`, the Photometry view embeds
  the widget below its results panel, and `attach_controllers(movers=...)`
  pushes the current project.
- **Tests**: `test_moving_objects.py` (audits extended: module set, pure vs
  widget source scan, subprocess no-gi boundary incl. `overlay_view` not
  eagerly imported, `_WIDGET_EXPORTS`, schema v2 round-trip, v1 migration,
  corrupt `mpc_match`), `test_moving_objects_mpc.py` (44 tests, mocked HTTP
  boundary), `test_moving_objects_overlay.py` (tonemap/render pixel
  assertions + headless controller over a tmp project), and
  `test_moving_objects_widgets.py` (gi-gated: wiring + widget behavior; skips
  headless like the other widget suites). Full suite green; ty + ruff clean.

