"""Link per-frame detections into tracks by nearest-neighbor in pixel space.

Pure-Python (module boundary per ``.scratch/moving-objects/spec.md``): no
GTK imports, no :mod:`seestar_controller.link` imports — a subprocess test
enforces the boundary. Stdlib + the pure :mod:`.detect` only.

What lives here:

- :class:`Track` — one linked track: the :class:`~.detect.Detection`
  points ordered by observation time, with convenience properties
  (``n_frames``, ``start`` / ``end``, ``duration_s``, ``motion_px``,
  ``speed_px_per_frame``);
- :func:`link_tracks` — greedy nearest-neighbor linking across consecutive
  frames in PIXEL coordinates.

Documented choices:

- **Pixel space.** The subs of a session share the same pointing (the
  scope does not move mid-session), so a mover's motion is a pixel-space
  displacement; the WCS (when present on the subs) is only used later, by
  :func:`~.flag.flag_linear_movers`, to express the speed in arcsec/frame.
- **Greedy nearest-neighbor, not a global optimizer.** Frames are
  processed in time order; for each frame every still-open track is paired
  with its nearest unused detection within ``max_motion_px`` (closest pairs
  first, each track/detection used at most once per frame). Unmatched
  detections start new tracks; unmatched tracks are closed. This is the
  standard simple tracker — good enough for isolated movers, documented so
  nobody expects Hungarian assignment.
- **``max_motion_px``** is the per-frame motion gate: a detection farther
  than that from a track's last point cannot extend it (a fast mover with a
  too-small gate splits into several short tracks — which then fail the
  ``min_frames`` cut in :func:`~.flag.flag_linear_movers`).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .detect import Detection, FrameDetections
from .errors import TrackError

#: Default per-frame motion gate in pixels when linking tracks.
DEFAULT_MAX_MOTION_PX = 10.0


@dataclass(frozen=True)
class Track:
    """One linked track: the detections ordered by observation time.

    - ``track_id`` — the 0-based id assigned by :func:`link_tracks` (tracks
      are numbered in the order of their first detection);
    - ``points`` — the :class:`~.detect.Detection` records, ordered by
      ``jd`` (ties by frame id).
    """

    track_id: int
    points: tuple[Detection, ...]

    @property
    def n_frames(self) -> int:
        """The number of frames the track spans."""
        return len(self.points)

    @property
    def start(self) -> Detection:
        """The first detection (earliest frame)."""
        return self.points[0]

    @property
    def end(self) -> Detection:
        """The last detection (latest frame)."""
        return self.points[-1]

    @property
    def duration_s(self) -> float:
        """The track's time span in seconds (``jd`` difference * 86400)."""
        return (self.points[-1].jd - self.points[0].jd) * 86400.0

    @property
    def motion_px(self) -> float:
        """The start -> end displacement in pixels."""
        return math.hypot(self.end.x - self.start.x, self.end.y - self.start.y)

    @property
    def speed_px_per_frame(self) -> float:
        """The mean speed in pixels per frame (``motion_px / (n-1)``);
        0.0 for a single-point track."""
        if len(self.points) < 2:
            return 0.0
        return self.motion_px / (len(self.points) - 1)

    @property
    def frame_ids(self) -> tuple[int, ...]:
        """The frame ids the track spans, in time order."""
        return tuple(p.frame_id for p in self.points)


def _flatten(detections: Iterable[Any]) -> list[Detection]:
    """Flatten the ``link_tracks`` input: accepts plain
    :class:`~.detect.Detection` records, :class:`~.detect.FrameDetections`
    (iterated), or a mix of both."""
    flat: list[Detection] = []
    for item in detections:
        if isinstance(item, Detection):
            flat.append(item)
        elif isinstance(item, FrameDetections):
            flat.extend(item.detections)
        else:
            raise TrackError(
                "detections must be Detection or FrameDetections records, "
                f"got {type(item).__name__}"
            )
    return flat


def _validate_max_motion(max_motion_px: Any) -> float:
    if (
        isinstance(max_motion_px, bool)
        or not isinstance(max_motion_px, (int, float))
        or not math.isfinite(max_motion_px)
        or max_motion_px <= 0
    ):
        raise TrackError(
            f"max_motion_px must be a positive number of pixels, got {max_motion_px!r}"
        )
    return float(max_motion_px)


def link_tracks(
    detections: Iterable[Any],
    max_motion_px: float = DEFAULT_MAX_MOTION_PX,
) -> list[Track]:
    """Link detections across frames into tracks (greedy nearest-neighbor
    in pixel coordinates).

    - ``detections`` — an iterable of :class:`~.detect.Detection` records
      and/or :class:`~.detect.FrameDetections` (e.g. the direct output of
      :func:`~.detect.detect_in_subs`); they are grouped by frame and
      ordered by observation time internally;
    - ``max_motion_px`` — the per-frame motion gate in pixels (default
      :data:`DEFAULT_MAX_MOTION_PX`).

    Returns the tracks ordered by their first detection's time, with
    ``track_id`` assigned 0..N-1 in that order. Single-frame detections
    become single-point tracks (they fail the ``min_frames`` cut later).
    """
    gate = _validate_max_motion(max_motion_px)
    flat = _flatten(detections)

    # group by frame, order frames by (jd, frame_id)
    by_frame: dict[int, list[Detection]] = {}
    frame_jd: dict[int, float] = {}
    for det in flat:
        by_frame.setdefault(det.frame_id, []).append(det)
        frame_jd[det.frame_id] = det.jd
    frame_order = sorted(by_frame, key=lambda fid: (frame_jd[fid], fid))

    open_tracks: list[list[Detection]] = []  # tracks still extendable
    done: list[list[Detection]] = []
    for fid in frame_order:
        frame_dets = by_frame[fid]
        # candidate pairs (distance, track index, detection index)
        pairs: list[tuple[float, int, int]] = []
        for ti, track in enumerate(open_tracks):
            last = track[-1]
            for di, det in enumerate(frame_dets):
                dist = math.hypot(det.x - last.x, det.y - last.y)
                if dist <= gate:
                    pairs.append((dist, ti, di))
        pairs.sort()
        used_tracks: set[int] = set()
        used_dets: set[int] = set()
        for _dist, ti, di in pairs:
            if ti in used_tracks or di in used_dets:
                continue
            open_tracks[ti].append(frame_dets[di])
            used_tracks.add(ti)
            used_dets.add(di)
        # unmatched detections start new tracks; unmatched tracks close
        still_open: list[list[Detection]] = []
        for ti, track in enumerate(open_tracks):
            if ti in used_tracks:
                still_open.append(track)
            else:
                done.append(track)
        for di, det in enumerate(frame_dets):
            if di not in used_dets:
                still_open.append([det])
        open_tracks = still_open
    done.extend(open_tracks)

    # number the tracks in the order of their first detection
    done.sort(key=lambda pts: (pts[0].jd, pts[0].frame_id, pts[0].x, pts[0].y))
    return [
        Track(track_id=i, points=tuple(sorted(pts, key=lambda d: (d.jd, d.frame_id))))
        for i, pts in enumerate(done)
    ]


__all__ = [
    "DEFAULT_MAX_MOTION_PX",
    "Track",
    "link_tracks",
]
