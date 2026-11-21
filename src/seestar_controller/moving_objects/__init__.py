"""Moving-object detection on a session's subs — pure core + MPC + overlay.

Pure-Python package (module boundary per ``.scratch/moving-objects/spec.md``):
**no GTK imports, no :mod:`seestar_controller.link` imports** — a subprocess
test enforces the boundary. Nothing here imports the widget layer or the
enhance pipeline; the only seestar imports are the pure
:mod:`seestar_controller.catalog` (coords + the read-only query cache) and
the store's read-only helpers (frame listing / session lookup / path
constants). Heavy numeric libraries (astropy, photutils) are imported lazily
INSIDE the functions that need them, so importing this package stays cheap
and gi-free.

Pipeline (issue 01 of moving-objects — detect, link, flag, persist):

1. :func:`~.detect.detect_in_subs` — per-frame source detection over the
   session's subs with photutils ``DAOStarFinder`` at a LOW threshold
   (movers are faint), WCS read from each sub's header when present
   (pixel-space detections always; sky positions only with a WCS);
2. :func:`~.link.link_tracks` — link the detections across frames into
   tracks by greedy nearest-neighbor in PIXEL coordinates (the subs of a
   session share the pointing, so pixel space is the right space; the WCS
   is only used later for arcsec/frame);
3. :func:`~.flag.flag_linear_movers` — flag the tracks that move linearly:
   least-squares line against the frame index, flagged when long enough,
   straight enough (residual cut), and fast enough (speed cut — this is
   what keeps static stars unflagged);
4. :func:`~.persist.save_tracks` / :func:`~.persist.load_tracks` — persist
   the tracks + mover reports per session as a FILE-BASED JSON sidecar
   ``<project>/subs/<session_id>/movers.json`` (the store schema is NOT
   touched).

Issue 02 adds the MPC lookup + the annotated overlay:

5. :mod:`.mpc` — the optional MPC Checker lookup for each flagged mover
   (the catalog module's HTTP+disk-cache pattern: one HTTP boundary
   :func:`~.mpc.fetch_mpc_checker_json`, a disk cache keyed by the rounded
   sky-track params + TTL, stale fallback on network failure; tests mock
   the boundary — no network). The pure matcher :func:`~.mpc.match_mover`
   compares the mover's sky track (mid position, rate, position angle)
   against the candidates; the match is persisted as ``mpc_match`` on the
   mover entry in the sidecar (schema_version 2 — v1 files migrate on read);
6. :mod:`.overlay` — the pure render + controller layer:
   :func:`~.overlay.render_frame_overlay` draws the tracks/movers on a
   tone-mapped frame (the annotated overlay), and
   :class:`~.overlay.MoverController` is the app-level shared controller
   (project -> sessions -> subs -> tracks/movers -> blinked frame) with
   frozen-snapshot listeners, driving the widget below.

Modules:

- :mod:`.errors` — the error taxonomy (:class:`~.errors.MovingObjectsError`
  base, :class:`~.errors.DetectionError`, :class:`~.errors.TrackError`,
  :class:`~.errors.PersistenceError`, and the issue-02
  :class:`~.errors.MpcError` / :class:`~.errors.MpcLookupError` /
  :class:`~.errors.MpcUnavailableError` / :class:`~.errors.MpcResponseError`);
- :mod:`.detect` — :class:`~.detect.Detection`,
  :class:`~.detect.FrameDetections`, :func:`~.detect.detect_in_subs`;
- :mod:`.link` — :class:`~.link.Track`, :func:`~.link.link_tracks`;
- :mod:`.flag` — :class:`~.flag.MoverReport` (with the optional
  ``mpc_match``), :func:`~.flag.flag_linear_movers`;
- :mod:`.persist` — :class:`~.persist.SessionTracks`,
  :func:`~.persist.tracks_path`, :func:`~.persist.save_tracks`,
  :func:`~.persist.load_tracks`;
- :mod:`.mpc` — :class:`~.mpc.MpcCandidate`, :class:`~.mpc.MpcMatch`,
  :func:`~.mpc.fetch_mpc_checker_json` (the single HTTP boundary),
  :func:`~.mpc.match_mover`, :func:`~.mpc.lookup_mover_cached`,
  :func:`~.mpc.lookup_and_persist`, ...;
- :mod:`.overlay` — :func:`~.overlay.tonemap_gray`,
  :func:`~.overlay.render_frame_overlay`, :class:`~.overlay.MoverSnapshot`,
  :class:`~.overlay.MoverController`;
- :mod:`.overlay_view` — the WIDGET layer (imports ``gi`` at module top,
  re-exported lazily below): :class:`~.overlay_view.MoverOverlayView`.
"""

from .detect import (
    DEFAULT_FWHM_PX,
    DEFAULT_THRESHOLD_SIGMA,
    Detection,
    FrameDetections,
    detect_in_subs,
)
from .errors import (
    DetectionError,
    MovingObjectsError,
    MpcError,
    MpcLookupError,
    MpcResponseError,
    MpcUnavailableError,
    PersistenceError,
    TrackError,
)
from .flag import (
    DEFAULT_MAX_RESIDUAL_PX,
    DEFAULT_MIN_FRAMES,
    DEFAULT_MIN_SPEED_PX_PER_FRAME,
    MoverReport,
    flag_linear_movers,
)
from .link import DEFAULT_MAX_MOTION_PX, Track, link_tracks
from .mpc import (
    DEFAULT_OBSCODE,
    MPC_CHECKER_URL,
    MpcCandidate,
    MpcMatch,
    build_checker_params,
    fetch_mpc_checker_json,
    lookup_and_persist,
    lookup_mover,
    lookup_mover_cached,
    lookup_movers,
    match_mover,
    mover_track_params,
    parse_mpc_response,
    with_matches,
)
from .overlay import (
    InvokeOnMain,
    MoverController,
    MoverListener,
    MoverSnapshot,
    render_frame_overlay,
    tonemap_gray,
)
from .persist import (
    MOVERS_FILE,
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    SessionTracks,
    load_tracks,
    save_tracks,
    tracks_path,
)

#: Widget-layer names re-exported lazily (importing the modules pulls in
#: ``gi`` — only the GUI path does).
_WIDGET_EXPORTS = {
    "MoverOverlayView": "overlay_view",
}


def __getattr__(name: str):
    if name in _WIDGET_EXPORTS:
        from importlib import import_module

        module = import_module(f".{_WIDGET_EXPORTS[name]}", __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DEFAULT_FWHM_PX",
    "DEFAULT_MAX_MOTION_PX",
    "DEFAULT_MAX_RESIDUAL_PX",
    "DEFAULT_MIN_FRAMES",
    "DEFAULT_MIN_SPEED_PX_PER_FRAME",
    "DEFAULT_OBSCODE",
    "DEFAULT_THRESHOLD_SIGMA",
    "MOVERS_FILE",
    "MPC_CHECKER_URL",
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "Detection",
    "DetectionError",
    "FrameDetections",
    "InvokeOnMain",
    "MoverController",
    "MoverListener",
    # widget layer (lazy, imports gi):
    "MoverOverlayView",
    "MoverReport",
    "MoverSnapshot",
    "MovingObjectsError",
    "MpcCandidate",
    "MpcError",
    "MpcLookupError",
    "MpcMatch",
    "MpcResponseError",
    "MpcUnavailableError",
    "PersistenceError",
    "SessionTracks",
    "Track",
    "TrackError",
    "build_checker_params",
    "detect_in_subs",
    "fetch_mpc_checker_json",
    "flag_linear_movers",
    "link_tracks",
    "load_tracks",
    "lookup_and_persist",
    "lookup_mover",
    "lookup_mover_cached",
    "lookup_movers",
    "match_mover",
    "mover_track_params",
    "parse_mpc_response",
    "render_frame_overlay",
    "save_tracks",
    "tonemap_gray",
    "tracks_path",
    "with_matches",
]
