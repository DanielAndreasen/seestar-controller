"""Flag linear movers among linked tracks (pure-Python linear fit).

Pure-Python (module boundary per ``.scratch/moving-objects/spec.md``): no
GTK imports, no :mod:`seestar_controller.link` imports — a subprocess test
enforces the boundary. Stdlib :mod:`math` + the pure :mod:`.link` /
:mod:`seestar_controller.catalog.coords` only.

What lives here:

- :class:`MoverReport` — one flagged linear mover: start/end position and
  time, speed in px/frame (and arcsec/frame when the subs carry a WCS),
  direction, and the linear-fit residual;
- :func:`flag_linear_movers` — the linearity test: least-squares line of
  ``(x, y)`` against the frame index, flagged when the track is long
  enough, straight enough, and fast enough.

Documented choices:

- **Fit against the frame index** ``i = 0..n-1`` (not against time): the
  subs of a session are taken at a roughly constant cadence, and the index
  keeps the fit a pure closed-form least squares (no numpy needed here).
  The per-frame speed is the slope magnitude ``hypot(b_x, b_y)``.
- **Residual** = RMS of the 2-D point-to-line distances
  ``sqrt(mean((x - fit_x)^2 + (y - fit_y)^2))`` — a curved track (e.g. a
  tumbling rocket body) has a large residual and is NOT flagged.
- **Three cuts** (all must pass): ``n_frames >= min_frames`` (default
  :data:`DEFAULT_MIN_FRAMES`), ``fit_residual_px <= max_residual_px``
  (default :data:`DEFAULT_MAX_RESIDUAL_PX`), and
  ``speed >= min_speed_px_per_frame`` (default
  :data:`DEFAULT_MIN_SPEED_PX_PER_FRAME`). The speed cut is what keeps
  static stars (a perfectly linear track with slope ~0) from being
  flagged as movers.
- **Direction** is in PIXEL space: ``atan2(dy, dx)`` in degrees, ``+x`` =
  image column (right), ``+y`` = image row (down in the usual FITS display
  convention), so 0 deg = moving right, 90 deg = moving down.
- **arcsec/frame** is reported only when every point of the track carries a
  WCS sky position (``ra_deg``/``dec_deg``); otherwise it is ``None`` — the
  pixel-space speed is always available.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from seestar_controller.catalog.coords import angular_distance

from .errors import TrackError
from .link import Track

#: Default minimum number of frames for a track to be flagged as a mover.
DEFAULT_MIN_FRAMES = 5

#: Default maximum linear-fit residual in pixels (RMS point-to-line distance).
DEFAULT_MAX_RESIDUAL_PX = 1.0

#: Default minimum speed in pixels per frame (keeps static stars unflagged).
DEFAULT_MIN_SPEED_PX_PER_FRAME = 0.5


@dataclass(frozen=True)
class MoverReport:
    """One flagged linear mover.

    Positions are in pixel coordinates (``x`` = column, ``y`` = row);
    ``ra_deg``/``dec_deg`` are the WCS sky positions when available.
    """

    track_id: int
    n_frames: int
    start_utc: str
    end_utc: str
    start_jd: float
    end_jd: float
    start_x: float
    start_y: float
    end_x: float
    end_y: float
    motion_px: float
    speed_px_per_frame: float
    direction_deg: float
    fit_residual_px: float
    speed_arcsec_per_frame: float | None
    start_ra_deg: float | None
    start_dec_deg: float | None
    end_ra_deg: float | None
    end_dec_deg: float | None


def _validate(name: str, value: Any, *, minimum: float, allow_equal: bool) -> float:
    ok = (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and (value >= minimum if allow_equal else value > minimum)
    )
    if not ok:
        cmp_op = ">=" if allow_equal else ">"
        raise TrackError(f"{name} must be a finite number {cmp_op} {minimum}, got {value!r}")
    return float(value)


def _linear_fit(xs: list[float], ys: list[float]) -> tuple[float, float, float, float]:
    """Least-squares line of ``(x, y)`` against the index ``i = 0..n-1``.

    Returns ``(slope_x, slope_y, intercept_x, intercept_y)`` — closed form,
    no numpy. ``n >= 2`` is guaranteed by the caller.
    """
    n = len(xs)
    sum_i = n * (n - 1) / 2.0
    sum_i2 = n * (n - 1) * (2 * n - 1) / 6.0
    denom = n * sum_i2 - sum_i * sum_i
    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_ix = sum(i * x for i, x in enumerate(xs))
    sum_iy = sum(i * y for i, y in enumerate(ys))
    slope_x = (n * sum_ix - sum_i * sum_x) / denom
    slope_y = (n * sum_iy - sum_i * sum_y) / denom
    intercept_x = (sum_x - slope_x * sum_i) / n
    intercept_y = (sum_y - slope_y * sum_i) / n
    return slope_x, slope_y, intercept_x, intercept_y


def flag_linear_movers(
    tracks: Iterable[Any],
    *,
    min_frames: int = DEFAULT_MIN_FRAMES,
    max_residual_px: float = DEFAULT_MAX_RESIDUAL_PX,
    min_speed_px_per_frame: float = DEFAULT_MIN_SPEED_PX_PER_FRAME,
) -> list[MoverReport]:
    """Flag the tracks that move linearly across the frames.

    - ``tracks`` — an iterable of :class:`~.link.Track` records (e.g. the
      output of :func:`~.link.link_tracks`);
    - ``min_frames`` — minimum track length in frames (default
      :data:`DEFAULT_MIN_FRAMES`);
    - ``max_residual_px`` — maximum linear-fit residual in pixels (default
      :data:`DEFAULT_MAX_RESIDUAL_PX`);
    - ``min_speed_px_per_frame`` — minimum speed in px/frame (default
      :data:`DEFAULT_MIN_SPEED_PX_PER_FRAME`).

    A track is flagged when it has at least ``min_frames`` points, its
    least-squares line against the frame index has an RMS residual of at
    most ``max_residual_px``, and its fitted speed is at least
    ``min_speed_px_per_frame``. Returns the reports ordered by track id.
    """
    if isinstance(min_frames, bool) or not isinstance(min_frames, int) or min_frames < 2:
        raise TrackError(f"min_frames must be an integer >= 2, got {min_frames!r}")
    max_res = _validate("max_residual_px", max_residual_px, minimum=0.0, allow_equal=True)
    min_speed = _validate(
        "min_speed_px_per_frame", min_speed_px_per_frame, minimum=0.0, allow_equal=True
    )

    reports: list[MoverReport] = []
    for track in tracks:
        if not isinstance(track, Track):
            raise TrackError(
                f"tracks must be Track records, got {type(track).__name__}"
            )
        pts = track.points
        n = len(pts)
        if n < min_frames:
            continue
        xs = [p.x for p in pts]
        ys = [p.y for p in pts]
        slope_x, slope_y, intercept_x, intercept_y = _linear_fit(xs, ys)
        residual = math.sqrt(
            sum(
                (x - (intercept_x + slope_x * i)) ** 2
                + (y - (intercept_y + slope_y * i)) ** 2
                for i, (x, y) in enumerate(zip(xs, ys))
            )
            / n
        )
        speed = math.hypot(slope_x, slope_y)
        if residual > max_res or speed < min_speed:
            continue
        start, end = pts[0], pts[-1]
        coords = [p.coord for p in pts]
        if all(c is not None for c in coords):
            speed_arcsec = (
                angular_distance(coords[0], coords[-1]) * 3600.0 / (n - 1)
            )
            start_ra, start_dec = coords[0].ra_deg, coords[0].dec_deg  # type: ignore[union-attr]
            end_ra, end_dec = coords[-1].ra_deg, coords[-1].dec_deg  # type: ignore[union-attr]
        else:
            speed_arcsec = None
            start_ra = start.ra_deg
            start_dec = start.dec_deg
            end_ra = end.ra_deg
            end_dec = end.dec_deg
        reports.append(
            MoverReport(
                track_id=track.track_id,
                n_frames=n,
                start_utc=start.utc,
                end_utc=end.utc,
                start_jd=start.jd,
                end_jd=end.jd,
                start_x=start.x,
                start_y=start.y,
                end_x=end.x,
                end_y=end.y,
                motion_px=math.hypot(end.x - start.x, end.y - start.y),
                speed_px_per_frame=speed,
                direction_deg=math.degrees(math.atan2(slope_y, slope_x)) % 360.0,
                fit_residual_px=residual,
                speed_arcsec_per_frame=speed_arcsec,
                start_ra_deg=start_ra,
                start_dec_deg=start_dec,
                end_ra_deg=end_ra,
                end_dec_deg=end_dec,
            )
        )
    reports.sort(key=lambda r: r.track_id)
    return reports


__all__ = [
    "DEFAULT_MAX_RESIDUAL_PX",
    "DEFAULT_MIN_FRAMES",
    "DEFAULT_MIN_SPEED_PX_PER_FRAME",
    "MoverReport",
    "flag_linear_movers",
]
