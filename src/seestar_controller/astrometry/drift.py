"""Pointing error + drift statistics — the PURE layer of astrometry ticket 02
(:mod:`seestar_controller.astrometry.drift`).

This is a PURE module of :mod:`seestar_controller.astrometry`: **no ``gi``
import** (the sandbox has no GTK, see docs/wiki/assumptions.md; a subprocess
test enforces the boundary) — stdlib + :mod:`seestar_controller.catalog` +
:mod:`seestar_controller.observing.queue` (the pure plan-queue model) +
:mod:`seestar_controller.store` only. No astropy is needed at all: the
angular math is plain spherical trigonometry in :mod:`math`.

What lives here (read-only consumer of the astrometry-01 ``frame_wcs`` store,
schema v8 — NO schema change):

- :class:`DriftError` — the pointing/drift computation failure (a
  :class:`~.errors.AstrometryError`, i.e. also a :class:`ValueError`): no
  requested target known, no solved frames in the session, or a bad
  ``entry`` argument;
- :func:`pointing_error` — the per-session pointing accuracy:
  ``pointing_error(project, session_id, entry=None)`` compares the session's
  REQUESTED target RA/Dec against the solved WCS centers of its frames
  (:func:`seestar_controller.store.wcs.list_solved_frames`) and returns a
  :class:`PointingError` whose ``error_arcsec`` is the great-circle
  separation between the requested position and the representative (median)
  solved center;
- :class:`DriftPoint` / :class:`DriftSeries` / :func:`drift_series` — the
  drift over the night: ``drift_series(project, session_id, entry=None)``
  returns the RA/Dec offset (arcsec, relative to the requested target) of
  every solved frame vs. its observation time (JD), plus the least-squares
  drift rates in arcsec/hour — the series a drift plot renders.

Requested-target resolution (documented choice — the queue itself is
in-memory only, so the requested position comes from the first available of):

1. the ``entry`` argument — the observing queue entry
   (:class:`~seestar_controller.observing.queue.QueueEntry`; its ``coord``
   is the requested J2000 position). A :class:`~seestar_controller.catalog.coords.J2000Coord`,
   a mapping with ``ra_deg``/``dec_deg``, or any object with those attributes
   is accepted too (duck-typed, same as the queue's own ``_as_coord``);
2. the project config's ``target`` placeholder
   (``project.config["target"]`` = ``{"name", "ra_deg", "dec_deg"}``, see
   :mod:`seestar_controller.store.project`);
3. the session frames' stored ``target`` text: the scope sidecar's
   ``{"ra": ..., "dec": ...}`` mapping is stored as its JSON encoding when it
   has no ``name`` (see :func:`seestar_controller.store.frames.store_frame`);
   RA is in HOURS there (the ``goto`` unit — the same convention as
   :func:`seestar_controller.frame_quality.trends.resolve_target_coords`).

When none of these yields a coordinate, :class:`DriftError` is raised —
never a silently wrong number.

Conventions:

- **Unknown session** raises
  :class:`~seestar_controller.store.errors.SessionNotFoundError` (a lookup,
  not a filter — propagated from
  :func:`seestar_controller.store.wcs.list_solved_frames`).
- **Missing-WCS frames are skipped and counted**: ``n_frames`` is every frame
  of the session (:func:`seestar_controller.store.frames.list_frames`),
  ``n_solved`` the frames with a WCS record, ``n_missing_wcs`` the rest —
  reported on both result types.
- **No solved frames at all** raises :class:`DriftError` (there is no solved
  center to compare against).
- **RA wraparound** is handled everywhere: offsets are wrapped to
  ``[-180, 180)`` deg and the median solved RA is unwrapped around the
  requested RA, so a target at RA 359.9° solved at 0.1° is a 0.2° error, not
  359.8°.
- **Drift rates** are ordinary least-squares slopes of the offset vs. hours
  since the first solved frame; None when fewer than 2 points or a zero time
  span.
"""

from __future__ import annotations

import json
import math
import numbers
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from seestar_controller.catalog.coords import CoordinateError, J2000Coord
from seestar_controller.gaia.query import ARCSEC_PER_DEG
from seestar_controller.observing.queue import QueueEntry
from seestar_controller.store.frames import Frame, list_frames
from seestar_controller.store.project import Project
from seestar_controller.store.wcs import FrameWcs, list_solved_frames

from .errors import AstrometryError


class DriftError(AstrometryError, ValueError):
    """A pointing-error / drift computation failed: no requested target is
    known for the session (pass the queue ``entry`` or set the project /
    frame target), the session has no solved frames, or the ``entry``
    argument is unusable. Subclasses both :class:`AstrometryError` and
    :class:`ValueError` — a bad input is a value problem, so callers may
    catch either."""


# --- angular math (pure stdlib spherical trigonometry) --------------------------------


def _wrap_180(delta_deg: float) -> float:
    """Wrap a RA difference in degrees to ``[-180, 180)``."""
    return (delta_deg + 180.0) % 360.0 - 180.0


def separation_arcsec(ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float) -> float:
    """The great-circle separation between two J2000 positions, in arcsec.

    The numerically stable atan2 form of the spherical law of cosines (no
    astropy needed)."""
    d_ra = math.radians(ra2_deg - ra1_deg)
    dec1 = math.radians(dec1_deg)
    dec2 = math.radians(dec2_deg)
    y = math.hypot(
        math.cos(dec2) * math.sin(d_ra),
        math.cos(dec1) * math.sin(dec2) - math.sin(dec1) * math.cos(dec2) * math.cos(d_ra),
    )
    x = math.sin(dec1) * math.sin(dec2) + math.cos(dec1) * math.cos(dec2) * math.cos(d_ra)
    return math.degrees(math.atan2(y, x)) * ARCSEC_PER_DEG


def _median(values: list[float]) -> float:
    """The median of a non-empty list of floats."""
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _median_ra(ras_deg: list[float], reference_deg: float) -> float:
    """The median RA, unwrapped around ``reference_deg`` so the 0/360 wrap
    does not split the sample; returned normalized to ``[0, 360)``."""
    unwrapped = [_wrap_180(ra - reference_deg) for ra in ras_deg]
    return (reference_deg + _median(unwrapped)) % 360.0


def _slope_per_hour(xs: list[float], ys: list[float]) -> float | None:
    """The ordinary least-squares slope of ``ys`` vs. ``xs`` (hours), or None
    when there are fewer than 2 points or the x span is zero."""
    n = len(xs)
    if n < 2:
        return None
    sum_x = math.fsum(xs)
    sum_y = math.fsum(ys)
    sum_xx = math.fsum(x * x for x in xs)
    sum_xy = math.fsum(x * y for x, y in zip(xs, ys))
    denominator = n * sum_xx - sum_x * sum_x
    if denominator == 0.0:
        return None
    return (n * sum_xy - sum_x * sum_y) / denominator


# --- requested-target resolution --------------------------------------------------------


def _coord_from_entry(entry: Any) -> J2000Coord:
    """The requested position from an observing queue entry (or a duck-typed
    equivalent): a :class:`QueueEntry` (its ``coord``), a
    :class:`J2000Coord`, a mapping with ``ra_deg``/``dec_deg``, or any object
    with ``ra_deg``/``dec_deg`` attributes. Raises :class:`DriftError` for an
    unresolved entry (name only) or invalid coordinates, :class:`TypeError`
    for anything else."""
    if isinstance(entry, QueueEntry):
        if entry.coord is None:
            raise DriftError(
                f"queue entry {entry.name!r} is unresolved (coord is None) — "
                "resolve the name first (observing.queue.resolve_names)"
            )
        return entry.coord
    if isinstance(entry, J2000Coord):
        return entry
    if isinstance(entry, Mapping):
        raw_ra = entry.get("ra_deg")
        raw_dec = entry.get("dec_deg")
        if raw_ra is None or raw_dec is None:
            raise DriftError(
                f"entry mapping needs numeric 'ra_deg'/'dec_deg', got {dict(entry)!r}"
            )
        try:
            return J2000Coord(ra_deg=raw_ra, dec_deg=raw_dec)
        except CoordinateError as exc:
            raise DriftError(f"invalid entry coordinates: {exc}") from exc
    coord = getattr(entry, "coord", None)
    if coord is not None and not isinstance(entry, (str, bytes)):
        return _coord_from_entry(coord)
    raw_ra = getattr(entry, "ra_deg", None)
    raw_dec = getattr(entry, "dec_deg", None)
    if raw_ra is not None and raw_dec is not None:
        try:
            return J2000Coord(ra_deg=raw_ra, dec_deg=raw_dec)
        except CoordinateError as exc:
            raise DriftError(f"invalid entry coordinates: {exc}") from exc
    raise TypeError(
        "entry must be a QueueEntry, a J2000Coord, a mapping with "
        f"ra_deg/dec_deg, or an object with those attributes, got {entry!r}"
    )


def _coord_from_config(project: Project) -> J2000Coord | None:
    """The requested position from the project config's ``target``
    placeholder (``{"name", "ra_deg", "dec_deg"}``), or None when unset or
    without coordinates."""
    target = project.config.get("target")
    if not isinstance(target, Mapping):
        return None
    raw_ra = target.get("ra_deg")
    raw_dec = target.get("dec_deg")
    if raw_ra is None or raw_dec is None:
        return None
    try:
        return J2000Coord(ra_deg=raw_ra, dec_deg=raw_dec)
    except CoordinateError:
        return None


def _coord_from_frame_targets(frames: list[Frame]) -> J2000Coord | None:
    """The requested position from the frames' stored ``target`` texts: the
    FIRST text that is a JSON object with numeric ``"ra"`` / ``"dec"`` entries
    wins (the scope sidecar's ``{"ra": ..., "dec": ...}`` mapping is stored as
    its JSON encoding when it has no ``name`` — see
    :func:`seestar_controller.store.frames.store_frame`). RA is in hours (the
    ``goto`` unit), Dec in degrees. None when no frame carries coordinates."""
    for frame in frames:
        text = frame.target
        if not isinstance(text, str):
            continue
        stripped = text.strip()
        if not stripped.startswith("{"):
            continue
        try:
            data = json.loads(stripped)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        raw_ra = data.get("ra")
        raw_dec = data.get("dec")
        if isinstance(raw_ra, bool) or isinstance(raw_dec, bool):
            continue
        if not isinstance(raw_ra, numbers.Real) or not isinstance(raw_dec, numbers.Real):
            continue
        ra, dec = float(raw_ra), float(raw_dec)
        if not math.isfinite(ra) or not math.isfinite(dec) or not -90.0 <= dec <= 90.0:
            continue
        try:
            return J2000Coord.from_hours(ra, dec)
        except CoordinateError:
            continue
    return None


def _requested_coord(
    project: Project, session_id: str, entry: Any, frames: list[Frame]
) -> J2000Coord:
    """Resolve the session's requested target position (see the module
    docstring for the priority order); raises :class:`DriftError` when no
    source yields a coordinate."""
    if entry is not None:
        return _coord_from_entry(entry)
    coord = _coord_from_config(project)
    if coord is not None:
        return coord
    coord = _coord_from_frame_targets(frames)
    if coord is not None:
        return coord
    raise DriftError(
        f"no requested target known for session {session_id!r} of project "
        f"{project.slug!r} — pass the observing queue entry (entry=...) or "
        "set the project config 'target' / a frame's target metadata"
    )


# --- result types -----------------------------------------------------------------------


@dataclass(frozen=True)
class PointingError:
    """The per-session pointing accuracy: requested target vs. solved WCS
    centers.

    - ``session_id`` — the session;
    - ``requested_ra_deg`` / ``requested_dec_deg`` — the requested target
      (J2000 degrees, RA in ``[0, 360)``);
    - ``solved_ra_deg`` / ``solved_dec_deg`` — the representative solved
      center: the per-axis MEDIAN of the session's solved frame centers (RA
      unwrapped around the requested RA, so the 0/360 wrap does not split
      the sample);
    - ``error_arcsec`` — the great-circle separation between the requested
      position and the representative solved center, in arcsec;
    - ``n_frames`` — every frame of the session;
    - ``n_solved`` — the frames with a solved WCS record;
    - ``n_missing_wcs`` — ``n_frames - n_solved`` (frames skipped for lack of
      a WCS).
    """

    session_id: str
    requested_ra_deg: float
    requested_dec_deg: float
    solved_ra_deg: float
    solved_dec_deg: float
    error_arcsec: float
    n_frames: int
    n_solved: int
    n_missing_wcs: int


@dataclass(frozen=True)
class DriftPoint:
    """One solved frame as a drift point: its offset from the requested
    target vs. its observation time.

    - ``frame_id`` — the ``frames`` row id;
    - ``jd`` / ``utc`` — the frame's observation time (Julian Date / UTC ISO);
    - ``hours`` — hours since the session's first solved frame;
    - ``ra_offset_arcsec`` — the RA offset in arcsec, wrapped to
      ``[-180, 180)`` deg and scaled by ``cos(requested_dec)`` (the small-
      circle correction), positive eastward;
    - ``dec_offset_arcsec`` — the Dec offset in arcsec, positive northward;
    - ``offset_arcsec`` — ``hypot(ra_offset, dec_offset)``, the total offset.
    """

    frame_id: int
    jd: float
    utc: str
    hours: float
    ra_offset_arcsec: float
    dec_offset_arcsec: float
    offset_arcsec: float


@dataclass(frozen=True)
class DriftSeries:
    """The drift of a session's solved frames over the night.

    - ``session_id`` — the session;
    - ``requested_ra_deg`` / ``requested_dec_deg`` — the requested target
      the offsets are relative to;
    - ``points`` — one :class:`DriftPoint` per solved frame, ordered by
      ``jd`` (frames without a WCS are skipped — see ``n_missing_wcs``);
    - ``n_frames`` / ``n_solved`` / ``n_missing_wcs`` — the frame counts;
    - ``ra_drift_arcsec_per_hour`` / ``dec_drift_arcsec_per_hour`` — the
      least-squares drift rates of the offsets vs. time (arcsec/hour), or
      None when fewer than 2 points or a zero time span.
    """

    session_id: str
    requested_ra_deg: float
    requested_dec_deg: float
    points: tuple[DriftPoint, ...]
    n_frames: int
    n_solved: int
    n_missing_wcs: int
    ra_drift_arcsec_per_hour: float | None
    dec_drift_arcsec_per_hour: float | None


# --- the two pure functions --------------------------------------------------------------


def _session_frames_and_solved(
    project: Project, session_id: str
) -> tuple[list[Frame], list[FrameWcs]]:
    """The session's frames (ordered by jd) and its solved-WCS records
    (ordered by the frames' jd). An unknown session raises
    :class:`~seestar_controller.store.errors.SessionNotFoundError` (from
    :func:`~seestar_controller.store.wcs.list_solved_frames`)."""
    if not isinstance(session_id, str):
        raise TypeError(f"session_id must be a string, got {session_id!r}")
    frames = list_frames(project, session_id)
    solved = list_solved_frames(project, session_id)
    return frames, list(solved)


def pointing_error(project: Project, session_id: str, entry: Any = None) -> PointingError:
    """The pointing accuracy of the session ``session_id`` in ``project``:
    the requested target position vs. the solved WCS centers of the
    session's frames, as a :class:`PointingError` (``error_arcsec`` is the
    separation in arcsec).

    - ``entry`` — the observing queue entry whose ``coord`` is the requested
      target (a :class:`~seestar_controller.observing.queue.QueueEntry`, a
      :class:`~seestar_controller.catalog.coords.J2000Coord`, or a duck-typed
      equivalent); None (default) resolves the requested position from the
      project config's ``target`` or the frames' ``target`` metadata (see the
      module docstring).

    Frames without a solved WCS are skipped and counted in
    ``n_missing_wcs``. Raises
    :class:`~seestar_controller.store.errors.SessionNotFoundError` for an
    unknown session and :class:`DriftError` when no requested target is known
    or the session has no solved frames.
    """
    frames, solved = _session_frames_and_solved(project, session_id)
    requested = _requested_coord(project, session_id, entry, frames)
    if not solved:
        raise DriftError(
            f"session {session_id!r} of project {project.slug!r} has no solved "
            f"frames ({len(frames)} frame(s), all without a WCS record) — "
            "plate-solve a frame first (astrometry.solve_session_frame)"
        )
    solved_ra = _median_ra([w.center_ra_deg for w in solved], requested.ra_deg)
    solved_dec = _median([w.center_dec_deg for w in solved])
    error = separation_arcsec(
        requested.ra_deg, requested.dec_deg, solved_ra, solved_dec
    )
    return PointingError(
        session_id=session_id,
        requested_ra_deg=requested.ra_deg,
        requested_dec_deg=requested.dec_deg,
        solved_ra_deg=solved_ra,
        solved_dec_deg=solved_dec,
        error_arcsec=error,
        n_frames=len(frames),
        n_solved=len(solved),
        n_missing_wcs=len(frames) - len(solved),
    )


def drift_series(project: Project, session_id: str, entry: Any = None) -> DriftSeries:
    """The drift of the session ``session_id`` in ``project`` over the night:
    the RA/Dec offset (arcsec, relative to the requested target) of every
    solved frame vs. its observation time, as a :class:`DriftSeries` (the
    series a drift plot renders, plus the least-squares drift rates in
    arcsec/hour).

    - ``entry`` — the observing queue entry with the requested position (see
      :func:`pointing_error`).

    Frames without a solved WCS are skipped and counted in
    ``n_missing_wcs``. Raises
    :class:`~seestar_controller.store.errors.SessionNotFoundError` for an
    unknown session and :class:`DriftError` when no requested target is known
    or the session has no solved frames.
    """
    frames, solved = _session_frames_and_solved(project, session_id)
    requested = _requested_coord(project, session_id, entry, frames)
    if not solved:
        raise DriftError(
            f"session {session_id!r} of project {project.slug!r} has no solved "
            f"frames ({len(frames)} frame(s), all without a WCS record) — "
            "plate-solve a frame first (astrometry.solve_session_frame)"
        )
    jd_by_frame = {frame.id: frame for frame in frames}
    cos_dec = math.cos(math.radians(requested.dec_deg))
    jd0 = min(jd_by_frame[w.frame_id].jd for w in solved)
    points: list[DriftPoint] = []
    for w in solved:  # already ordered by the frames' jd
        frame = jd_by_frame[w.frame_id]
        ra_offset = _wrap_180(w.center_ra_deg - requested.ra_deg) * cos_dec * ARCSEC_PER_DEG
        dec_offset = (w.center_dec_deg - requested.dec_deg) * ARCSEC_PER_DEG
        points.append(
            DriftPoint(
                frame_id=w.frame_id,
                jd=frame.jd,
                utc=frame.utc,
                hours=(frame.jd - jd0) * 24.0,
                ra_offset_arcsec=ra_offset,
                dec_offset_arcsec=dec_offset,
                offset_arcsec=math.hypot(ra_offset, dec_offset),
            )
        )
    hours = [p.hours for p in points]
    return DriftSeries(
        session_id=session_id,
        requested_ra_deg=requested.ra_deg,
        requested_dec_deg=requested.dec_deg,
        points=tuple(points),
        n_frames=len(frames),
        n_solved=len(solved),
        n_missing_wcs=len(frames) - len(solved),
        ra_drift_arcsec_per_hour=_slope_per_hour(hours, [p.ra_offset_arcsec for p in points]),
        dec_drift_arcsec_per_hour=_slope_per_hour(hours, [p.dec_offset_arcsec for p in points]),
    )
