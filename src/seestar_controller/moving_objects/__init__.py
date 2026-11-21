"""Moving-object detection on a session's subs — pure core (issue 01).

Pure-Python package (module boundary per ``.scratch/moving-objects/spec.md``):
**no GTK imports, no :mod:`seestar_controller.link` imports** — a subprocess
test enforces the boundary. Nothing here imports the widget layer or the
enhance pipeline; the only seestar imports are the pure
:mod:`seestar_controller.catalog.coords` (great-circle distance for the
arcsec/frame speed) and the store's read-only helpers (frame listing /
session lookup / path constants). Heavy numeric libraries (numpy, astropy,
photutils) are imported lazily INSIDE the functions that need them, so
importing this package stays cheap and gi-free.

Pipeline (issue 01 of moving-objects — detect, link, flag):

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

Modules:

- :mod:`.errors` — the error taxonomy (:class:`~.errors.MovingObjectsError`
  base, :class:`~.errors.DetectionError`, :class:`~.errors.TrackError`,
  :class:`~.errors.PersistenceError`);
- :mod:`.detect` — :class:`~.detect.Detection`,
  :class:`~.detect.FrameDetections`, :func:`~.detect.detect_in_subs`;
- :mod:`.link` — :class:`~.link.Track`, :func:`~.link.link_tracks`;
- :mod:`.flag` — :class:`~.flag.MoverReport`, :func:`~.flag.flag_linear_movers`;
- :mod:`.persist` — :class:`~.persist.SessionTracks`,
  :func:`~.persist.tracks_path`, :func:`~.persist.save_tracks`,
  :func:`~.persist.load_tracks`.
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
from .persist import (
    MOVERS_FILE,
    SCHEMA_VERSION,
    SessionTracks,
    load_tracks,
    save_tracks,
    tracks_path,
)

__all__ = [
    "DEFAULT_FWHM_PX",
    "DEFAULT_MAX_MOTION_PX",
    "DEFAULT_MAX_RESIDUAL_PX",
    "DEFAULT_MIN_FRAMES",
    "DEFAULT_MIN_SPEED_PX_PER_FRAME",
    "DEFAULT_THRESHOLD_SIGMA",
    "MOVERS_FILE",
    "SCHEMA_VERSION",
    "Detection",
    "DetectionError",
    "FrameDetections",
    "MoverReport",
    "MovingObjectsError",
    "PersistenceError",
    "SessionTracks",
    "Track",
    "TrackError",
    "detect_in_subs",
    "flag_linear_movers",
    "link_tracks",
    "load_tracks",
    "save_tracks",
    "tracks_path",
]
