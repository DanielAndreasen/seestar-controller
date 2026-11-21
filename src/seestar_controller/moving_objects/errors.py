"""Error taxonomy of the moving-objects pure core.

Pure-Python (module boundary per ``.scratch/moving-objects/spec.md``): no
GTK imports, no :mod:`seestar_controller.link` imports — a subprocess test
enforces the boundary. Every failure of this package raises one of these
types with a clear, actionable message — never a raw traceback.
"""

from __future__ import annotations


class MovingObjectsError(Exception):
    """Base class for every moving-objects failure."""


class DetectionError(MovingObjectsError, ValueError):
    """Per-sub detection failed — a bad image, a bad argument, or a FITS
    file that cannot be read."""


class TrackError(MovingObjectsError, ValueError):
    """Track linking / mover flagging got a bad argument (e.g. a
    non-positive ``max_motion_px`` or ``min_frames``)."""


class PersistenceError(MovingObjectsError, OSError):
    """The per-session tracks file (``subs/<session>/movers.json``) could
    not be written, is missing, or is corrupt/unreadable."""
