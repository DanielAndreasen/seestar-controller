"""Quality trends — the PURE trend computation of frame-quality ticket 02
(:mod:`seestar_controller.frame_quality.trends`).

This is a PURE module of :mod:`seestar_controller.frame_quality`: **no ``gi``
import** (the sandbox has no GTK, see docs/wiki/assumptions.md; a subprocess
test enforces the boundary) — stdlib + :mod:`numpy` +
:mod:`seestar_controller.store` + :mod:`.metrics` only. The heavy
:mod:`astropy` import (AltAz airmass) is LAZY inside
:func:`airmass_series`, the same pattern as
:func:`.metrics.score_frame`'s astropy/photutils imports. The widget layer
(:mod:`.quality_view`) imports ``gi`` at module top and is never imported by
this module or eagerly by the package ``__init__``.

What lives here:

- :class:`TrendsError` — the trend-layer failure (a
  :class:`~.metrics.QualityError`, i.e. a :class:`ValueError`);
- :class:`Observer` — the observer's location (latitude/longitude/elevation)
  used for the airmass; :data:`DEFAULT_OBSERVER` is the Greenwich placeholder
  (there is no settings key for the site yet — documented choice);
- :func:`airmass_series` — the airmass (sec z) of a target at each frame time:
  target coords (RA in hours, Dec in degrees, J2000 — the scope sidecar
  convention) + Julian Dates in, airmass per JD out, computed with
  ``astropy.coordinates.AltAz`` (lazy import) at the observer's location.
  The airmass is clamped to ``[1, MAX_AIRMASS]``; below-horizon times get
  :data:`MAX_AIRMASS` (the target is not observable then — the curve
  saturates at the horizon value instead of going negative);
- :func:`resolve_target_coords` — extract ``(ra_hours, dec_deg)`` from the
  frames' stored ``target`` text (the scope sidecar's
  ``{"ra": ..., "dec": ...}`` mapping is stored as its JSON encoding when it
  has no ``name`` — see :func:`seestar_controller.store.frames.store_frame`);
- :class:`TrendPoint` / :class:`SessionTrends` / :func:`session_trends` — the
  per-session series extraction from the STORED metrics:
  :func:`seestar_controller.store.quality.session_quality` (the scored frames
  ordered by ``jd``) joined with :func:`seestar_controller.store.frames.list_frames`
  (the frames' ``jd`` / ``utc`` / ``target``), plus the airmass series when the
  target coords are known. The summary (median FWHM, % rejected, ...) mirrors
  :class:`~seestar_controller.store.quality.SessionQuality`;
- the **plot transform math** (pure, unit-testable headless — the widget only
  feeds it events): :class:`TrendWindow` (the visible data window
  ``x_min``/``x_max``/``y_min``/``y_max`` — x is JD, y is the plotted metric,
  **NOT** inverted, unlike the lightcurve's mag axis),
  :func:`full_trend_window` (the data range padded, the reset view),
  :func:`trend_to_screen` / :func:`screen_to_trend` (the data<->screen
  mapping; ``geom`` is duck-typed — the widget passes the analysis
  lightcurve view's :class:`~seestar_controller.analysis.lightcurve.PlotGeometry`);
- :class:`QualitySnapshot` / :class:`QualityController` — the app-level shared
  controller (the app owns one, following the shared viewer controller's
  project like the lightcurve controller does): :attr:`snapshot` (the frozen
  :class:`QualitySnapshot` — the project's sessions, the selected session and
  its :class:`SessionTrends`, and a never-raising ``error``),
  :meth:`refresh` / :meth:`set_project` / :meth:`attach_viewer` /
  :meth:`sessions` / :meth:`select_session` / :meth:`set_observer`, and
  listeners with frozen snapshots marshalled via ``invoke_on_main`` (the app
  binds ``GLib.idle_add``; the default calls directly, so tests are
  synchronous).

Conventions (documented choices):

- **Threading / sqlite thread-affinity.** Same as
  :class:`seestar_controller.analysis.lightcurve.LightcurveController`: every
  STORE call goes through :meth:`QualityController._call_store` — it runs
  directly on the db-owning thread (captured when the project is set) and is
  otherwise marshalled through ``invoke_on_main`` with a wait.
- **Session selection.** The snapshot's ``session_id`` is the explicitly
  selected one when it still exists, else the FIRST session (ordered by
  ``started_at``) — the widget's dropdown defaults to index 0 the same way.
- **Unknown session.** :func:`session_trends` raises
  :class:`~seestar_controller.store.errors.SessionNotFoundError` (a lookup,
  not a filter — same as :func:`~seestar_controller.store.quality.session_quality`);
  the controller's snapshot NEVER raises — a failure lands in
  :attr:`QualitySnapshot.error`.
- **No target coords -> no airmass.** When no frame of the session carries a
  JSON ``{"ra", "dec"}`` target, :attr:`SessionTrends.airmass` is empty and
  :attr:`SessionTrends.has_airmass` is False — the widget shows the airmass
  plot's placeholder instead of a wrong curve.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from seestar_controller.store.frames import list_frames
from seestar_controller.store.project import Project
from seestar_controller.store.quality import session_quality
from seestar_controller.store.sessions import list_sessions

from .metrics import QualityError

logger = logging.getLogger(__name__)


class TrendsError(QualityError):
    """A quality-trends failure: invalid airmass/observer/window arguments,
    or a store failure while building the trends (a
    :class:`~.metrics.QualityError`, i.e. a :class:`ValueError`)."""


# --------------------------------------------------------------------------------------
# Observer + airmass
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Observer:
    """The observer's location on Earth for the airmass computation.

    - ``latitude_deg`` — in ``[-90, 90]``;
    - ``longitude_deg`` — in ``[-180, 180]`` (east positive);
    - ``elevation_m`` — the height above the reference ellipsoid in meters
      (default 0).
    """

    latitude_deg: float
    longitude_deg: float
    elevation_m: float = 0.0

    def __post_init__(self) -> None:
        for name in ("latitude_deg", "longitude_deg", "elevation_m"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TrendsError(f"{name} must be a number, got {value!r}")
            if not math.isfinite(float(value)):
                raise TrendsError(f"{name} must be finite, got {value!r}")
        if not -90.0 <= self.latitude_deg <= 90.0:
            raise TrendsError(f"latitude_deg must be in [-90, 90], got {self.latitude_deg!r}")
        if not -180.0 <= self.longitude_deg <= 180.0:
            raise TrendsError(
                f"longitude_deg must be in [-180, 180], got {self.longitude_deg!r}"
            )


#: The default observer: the Greenwich meridian at sea level. There is no
#: settings key for the observing site yet, so this is a documented
#: placeholder — the app-level controller's :meth:`QualityController.set_observer`
#: replaces it.
DEFAULT_OBSERVER = Observer(latitude_deg=0.0, longitude_deg=0.0, elevation_m=0.0)

#: The airmass clamp: sec z at the horizon is unbounded, so the curve is
#: saturated at this value (the usual "horizon" cutoff of photometry) and
#: below-horizon times get exactly this value.
MAX_AIRMASS = 40.0


def _finite_number(value: Any, what: str) -> float:
    """Coerce ``value`` to a finite float (bools rejected); :class:`TrendsError`
    otherwise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrendsError(f"{what} must be a number, got {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise TrendsError(f"{what} must be finite, got {value!r}")
    return result


def airmass_series(
    ra_hours: float,
    dec_deg: float,
    jds: Iterable[float],
    *,
    observer: Observer = DEFAULT_OBSERVER,
) -> tuple[float, ...]:
    """The airmass (sec z) of the target ``(ra_hours, dec_deg)`` (J2000; RA in
    hours, Dec in degrees — the scope sidecar convention) at each Julian Date
    in ``jds``, computed with ``astropy.coordinates.AltAz`` at ``observer``'s
    location (the astropy import is lazy — this module imports no astropy).

    The result is clamped to ``[1, MAX_AIRMASS]``; times with the target below
    the horizon get :data:`MAX_AIRMASS`. An empty ``jds`` yields ``()``.

    Raises :class:`TrendsError` when ``ra_hours``/``dec_deg``/a jd is not a
    finite number, ``dec_deg`` is outside ``[-90, 90]``, or ``observer`` is not
    an :class:`Observer`.
    """
    ra = _finite_number(ra_hours, "ra_hours") % 24.0
    dec = _finite_number(dec_deg, "dec_deg")
    if not -90.0 <= dec <= 90.0:
        raise TrendsError(f"dec_deg must be in [-90, 90], got {dec_deg!r}")
    if not isinstance(observer, Observer):
        raise TrendsError(f"observer must be an Observer, got {observer!r}")
    jd_values = [_finite_number(jd, "jd") for jd in jds]
    if not jd_values:
        return ()

    import astropy.units as u
    import numpy as np
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time

    times = Time(np.asarray(jd_values, dtype=np.float64), format="jd", scale="utc")
    location = EarthLocation(
        lat=observer.latitude_deg * u.deg,
        lon=observer.longitude_deg * u.deg,
        height=observer.elevation_m * u.m,
    )
    coord = SkyCoord(ra=ra * u.hourangle, dec=dec * u.deg, frame="icrs")
    altaz = coord.transform_to(AltAz(obstime=times, location=location))
    altitudes = np.asarray(altaz.alt.deg, dtype=np.float64)
    secz = np.asarray(altaz.secz, dtype=np.float64)
    result: list[float] = []
    for alt, sec in zip(altitudes, secz):
        if alt <= 0.0 or not math.isfinite(sec) or sec <= 0.0:
            result.append(MAX_AIRMASS)  # below the horizon: saturate
        else:
            result.append(min(max(float(sec), 1.0), MAX_AIRMASS))
    return tuple(result)


def resolve_target_coords(targets: Iterable[str | None]) -> tuple[float, float] | None:
    """Extract ``(ra_hours, dec_deg)`` from the frames' stored ``target``
    texts: the FIRST text that is a JSON object with numeric ``"ra"`` /
    ``"dec"`` entries wins (the scope sidecar's
    ``{"ra": ..., "dec": ...}`` mapping is stored as its JSON encoding when it
    has no ``name`` — see
    :func:`seestar_controller.store.frames.store_frame`). RA is in hours, Dec
    in degrees. Returns None when no target carries coordinates."""
    for text in targets:
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
        if not isinstance(raw_ra, (int, float)) or not isinstance(raw_dec, (int, float)):
            continue
        ra, dec = float(raw_ra), float(raw_dec)
        if not math.isfinite(ra) or not math.isfinite(dec) or not -90.0 <= dec <= 90.0:
            continue
        return ra % 24.0, dec
    return None


# --------------------------------------------------------------------------------------
# Series extraction from the stored metrics
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TrendPoint:
    """One scored frame of a session as a trend point.

    - ``frame_id`` — the ``frames`` row id;
    - ``jd`` / ``utc`` — the frame's observation time (Julian Date / UTC ISO);
    - ``fwhm`` — the estimated FWHM in pixels, or None when no star was found;
    - ``bg_median`` / ``bg_std`` — the sigma-clipped background median / std;
    - ``star_count`` — the detected star count;
    - ``flagged`` — the outlier flag (drawn marked, e.g. red);
    - ``flag_reasons`` — the metric names that flagged the frame.
    """

    frame_id: int
    jd: float
    utc: str
    fwhm: float | None
    bg_median: float
    bg_std: float
    star_count: int
    flagged: bool
    flag_reasons: tuple[str, ...]


@dataclass(frozen=True)
class SessionTrends:
    """The per-session quality trends of one session (frozen).

    - ``session_id`` — the session;
    - ``target`` — the session's target name (the first non-None frame
      ``target``), or None;
    - ``target_coords`` — ``(ra_hours, dec_deg)`` resolved from the frames'
      target texts (or given explicitly), or None;
    - ``observer`` — the :class:`Observer` the airmass was computed at;
    - ``points`` — the :class:`TrendPoint`\\ s ordered by ``jd`` (the
      :func:`~seestar_controller.store.quality.session_quality` order);
    - ``airmass`` — the airmass per point (same length as ``points``), EMPTY
      when ``target_coords`` is None.

    Summary properties: :attr:`frame_count`, :attr:`flagged`,
    :attr:`flagged_count`, :attr:`rejected_percent` (``100 * flagged / total``,
    0.0 when empty), :attr:`median_fwhm` (None when no frame has a FWHM),
    :attr:`median_bg_median`, :attr:`median_bg_std`, :attr:`median_star_count`,
    :attr:`jds`, and :attr:`has_airmass`.
    """

    session_id: str
    target: str | None
    target_coords: tuple[float, float] | None
    observer: Observer
    points: tuple[TrendPoint, ...]
    airmass: tuple[float, ...]

    @property
    def frame_count(self) -> int:
        """The number of scored frames in the session."""
        return len(self.points)

    @property
    def flagged(self) -> tuple[TrendPoint, ...]:
        """The flagged (rejected) points of the session."""
        return tuple(p for p in self.points if p.flagged)

    @property
    def flagged_count(self) -> int:
        """The number of flagged (rejected) frames."""
        return len(self.flagged)

    @property
    def rejected_percent(self) -> float:
        """The rejected fraction in percent (0.0 when no scored frames)."""
        if not self.points:
            return 0.0
        return 100.0 * len(self.flagged) / len(self.points)

    @property
    def median_fwhm(self) -> float | None:
        """The median FWHM (pixels) over the frames that have one, or None."""
        values = [p.fwhm for p in self.points if p.fwhm is not None]
        if not values:
            return None
        return float(statistics.median(values))

    @property
    def median_bg_median(self) -> float | None:
        """The median background level over the scored frames, or None."""
        if not self.points:
            return None
        return float(statistics.median(p.bg_median for p in self.points))

    @property
    def median_bg_std(self) -> float | None:
        """The median background std over the scored frames, or None."""
        if not self.points:
            return None
        return float(statistics.median(p.bg_std for p in self.points))

    @property
    def median_star_count(self) -> float | None:
        """The median detected star count over the scored frames, or None."""
        if not self.points:
            return None
        return float(statistics.median(p.star_count for p in self.points))

    @property
    def jds(self) -> tuple[float, ...]:
        """The points' Julian Dates (ordered)."""
        return tuple(p.jd for p in self.points)

    @property
    def has_airmass(self) -> bool:
        """Whether the airmass series is present (target coords known)."""
        return len(self.airmass) == len(self.points) and bool(self.points)


def session_trends(
    project: Project,
    session_id: str,
    *,
    observer: Observer = DEFAULT_OBSERVER,
    target_coords: tuple[float, float] | None = None,
) -> SessionTrends:
    """The quality trends of the session ``session_id`` in ``project``:
    the stored metrics (:func:`seestar_controller.store.quality.session_quality`,
    ordered by ``jd``) joined with the frames' ``jd`` / ``utc`` / ``target``
    (:func:`seestar_controller.store.frames.list_frames`), plus the airmass
    series when the target coords are known.

    - ``observer`` — the :class:`Observer` for the airmass (default
      :data:`DEFAULT_OBSERVER`);
    - ``target_coords`` — explicit ``(ra_hours, dec_deg)``; when None it is
      resolved from the session frames' ``target`` texts via
      :func:`resolve_target_coords`.

    Raises :class:`~seestar_controller.store.errors.SessionNotFoundError` for
    an unknown session id (a lookup, not a filter) and :class:`TrendsError` for
    invalid arguments. A session with no scored frames yields empty ``points``
    (and empty ``airmass``), medians None, ``rejected_percent == 0.0``.
    """
    if not isinstance(session_id, str):
        raise TypeError(f"session_id must be a string, got {session_id!r}")
    if not isinstance(observer, Observer):
        raise TypeError(f"observer must be an Observer, got {observer!r}")
    if target_coords is not None:
        if (
            not isinstance(target_coords, tuple)
            or len(target_coords) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in target_coords)
        ):
            raise TrendsError(
                f"target_coords must be a (ra_hours, dec_deg) pair of numbers, "
                f"got {target_coords!r}"
            )
        ra, dec = float(target_coords[0]), float(target_coords[1])
        if not math.isfinite(ra) or not math.isfinite(dec) or not -90.0 <= dec <= 90.0:
            raise TrendsError(f"target_coords must be finite with dec in [-90, 90], got {target_coords!r}")
        target_coords = (ra % 24.0, dec)

    quality = session_quality(project, session_id)  # raises SessionNotFoundError
    frames = {f.id: f for f in list_frames(project, session_id=session_id)}
    points: list[TrendPoint] = []
    for record in quality.frames:
        frame = frames.get(record.frame_id)
        if frame is None:  # cannot happen (FK join), but never crash
            continue
        points.append(
            TrendPoint(
                frame_id=record.frame_id,
                jd=frame.jd,
                utc=frame.utc,
                fwhm=record.fwhm,
                bg_median=record.bg_median,
                bg_std=record.bg_std,
                star_count=record.star_count,
                flagged=record.flagged,
                flag_reasons=record.flag_reasons,
            )
        )
    target = next((f.target for f in frames.values() if f.target is not None), None)
    if target_coords is None:
        target_coords = resolve_target_coords(f.target for f in frames.values())
    airmass: tuple[float, ...] = ()
    if target_coords is not None and points:
        airmass = airmass_series(
            target_coords[0], target_coords[1], [p.jd for p in points], observer=observer
        )
    return SessionTrends(
        session_id=session_id,
        target=target,
        target_coords=target_coords,
        observer=observer,
        points=tuple(points),
        airmass=airmass,
    )


# --------------------------------------------------------------------------------------
# Plot transform math (pure — the widget only feeds it events)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TrendWindow:
    """The visible data window of a trend plot: ``x_min``/``x_max`` on the x
    axis (JD) and ``y_min``/``y_max`` on the y axis (the metric).

    Unlike the lightcurve's mag axis, the y axis is NOT inverted: larger
    values are drawn higher. ``x_max > x_min`` and ``y_max > y_min`` are
    enforced.
    """

    x_min: float
    x_max: float
    y_min: float
    y_max: float

    def __post_init__(self) -> None:
        for name in ("x_min", "x_max", "y_min", "y_max"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TrendsError(f"{name} must be a number, got {value!r}")
            if not math.isfinite(float(value)):
                raise TrendsError(f"{name} must be finite, got {value!r}")
        if not self.x_max > self.x_min:
            raise TrendsError(f"x_max must be > x_min, got [{self.x_min}, {self.x_max}]")
        if not self.y_max > self.y_min:
            raise TrendsError(f"y_max must be > y_min, got [{self.y_min}, {self.y_max}]")

    @property
    def x_span(self) -> float:
        """The visible x range."""
        return self.x_max - self.x_min

    @property
    def y_span(self) -> float:
        """The visible y range."""
        return self.y_max - self.y_min


#: The default window when there is no data (the placeholder is drawn then,
#: but the transforms must still not crash).
DEFAULT_TREND_WINDOW = TrendWindow(x_min=0.0, x_max=1.0, y_min=0.0, y_max=1.0)


def full_trend_window(xs: Iterable[float], ys: Iterable[float]) -> TrendWindow:
    """The full data window of ``(xs, ys)`` (the reset view): the x range of
    ``xs`` and the y range of ``ys``, each padded by 5% on both sides
    (degenerate ranges expanded by ±1e-9 / ±1e-6). Empty input yields
    :data:`DEFAULT_TREND_WINDOW`."""
    xs = [float(x) for x in xs]
    ys = [float(y) for y in ys]
    if not xs or not ys:
        return DEFAULT_TREND_WINDOW
    x_min, x_max = min(xs), max(xs)
    if x_max - x_min < 1e-12:
        x_min, x_max = x_min - 1e-9, x_max + 1e-9
    else:
        pad = 0.05 * (x_max - x_min)
        x_min, x_max = x_min - pad, x_max + pad
    y_min, y_max = min(ys), max(ys)
    if y_max - y_min < 1e-12:
        y_min, y_max = y_min - 1e-6, y_max + 1e-6
    else:
        pad = 0.05 * (y_max - y_min)
        y_min, y_max = y_min - pad, y_max + pad
    return TrendWindow(x_min=x_min, x_max=x_max, y_min=y_min, y_max=y_max)


def trend_to_screen(window: TrendWindow, geom: Any, x: float, y: float) -> tuple[float, float]:
    """Map a data point ``(x, y)`` to plot pixel coordinates ``(px, py)``.

    ``geom`` is duck-typed: it needs ``margin_left``, ``margin_top``,
    ``plot_width`` and ``plot_height`` (the analysis lightcurve view's
    :class:`~seestar_controller.analysis.lightcurve.PlotGeometry` provides
    exactly those). The y axis is NOT inverted: ``window.y_max`` maps to
    ``geom.margin_top`` (the top).
    """
    px = geom.margin_left + (x - window.x_min) / window.x_span * geom.plot_width
    py = geom.margin_top + geom.plot_height - (y - window.y_min) / window.y_span * geom.plot_height
    return px, py


def screen_to_trend(window: TrendWindow, geom: Any, px: float, py: float) -> tuple[float, float]:
    """Map plot pixel coordinates ``(px, py)`` back to data ``(x, y)`` — the
    exact inverse of :func:`trend_to_screen`."""
    x = window.x_min + (px - geom.margin_left) / geom.plot_width * window.x_span
    y = window.y_min + (geom.margin_top + geom.plot_height - py) / geom.plot_height * window.y_span
    return x, y


# --------------------------------------------------------------------------------------
# The controller
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class QualitySnapshot:
    """The frozen snapshot of a :class:`QualityController`, handed to the
    listeners on every change.

    - ``sessions`` — the project's session ids ordered by ``started_at``;
    - ``session_id`` — the selected session (the explicit selection when it
      still exists, else the first session), or None when there are none;
    - ``trends`` — the :class:`SessionTrends` of the selected session, or None
      when there is no session (or the read failed — see ``error``);
    - ``error`` — the last read error message, else None.
    """

    sessions: tuple[str, ...]
    session_id: str | None
    trends: SessionTrends | None
    error: str | None


#: A quality listener: called with a frozen :class:`QualitySnapshot` on any
#: change (always marshalled via ``invoke_on_main``).
QualityListener = Callable[[QualitySnapshot], None]

#: Marshals a zero-argument callback onto the "main" thread. The widget layer
#: binds this to ``GLib.idle_add``; the default calls it directly.
InvokeOnMain = Callable[[Callable[[], None]], Any]


def _direct_invoke(callback: Callable[[], None]) -> None:
    """The default ``invoke_on_main``: run the callback directly (tests)."""
    callback()


#: How long :meth:`QualityController._call_store` waits for a marshalled store
#: call on the db-owning thread before giving up (same value as the analysis
#: lightcurve controller's, re-declared: this package must not import it).
DB_CALL_TIMEOUT_S = 60.0


class QualityController:
    """The app-level session-quality controller (frame-quality ticket 02).

    - ``project`` — the store project to read (None = follow the attached
      viewer controller's project, see :meth:`attach_viewer`);
    - ``viewer_controller`` — an optional duck-typed viewer controller (the
      shared :class:`~seestar_controller.enhance.viewer_controller.ViewerController`
      — NOT imported here): its project is adopted whenever no explicit
      project is set, so the quality trends follow the currently open project;
    - ``invoke_on_main`` — the marshalling callable for listener notifications
      AND for store calls made from a non-db thread (the app binds
      ``GLib.idle_add``; the default calls directly);
    - ``observer`` — the :class:`Observer` the airmass is computed at (default
      :data:`DEFAULT_OBSERVER`); :meth:`set_observer` replaces it.

    Operations: :attr:`snapshot` (the frozen :class:`QualitySnapshot` — never
    raises), :meth:`refresh` (reload + notify), :meth:`set_project`,
    :meth:`attach_viewer` / :meth:`detach_viewer`, :meth:`sessions` (the
    project's session ids), :meth:`select_session` (the session the trends are
    shown for), :meth:`set_observer`, and listeners with frozen snapshots.
    """

    def __init__(
        self,
        project: Any | None = None,
        viewer_controller: Any | None = None,
        *,
        invoke_on_main: InvokeOnMain | None = None,
        observer: Observer = DEFAULT_OBSERVER,
    ) -> None:
        if not isinstance(observer, Observer):
            raise TypeError(f"observer must be an Observer, got {observer!r}")
        self._project: Any | None = None
        self._viewer_controller: Any | None = None
        self._invoke_on_main: InvokeOnMain = invoke_on_main or _direct_invoke
        self._observer = observer
        self._session_id: str | None = None
        self._lock = threading.Lock()
        self._listeners: list[QualityListener] = []
        self._db_thread_id: int | None = None
        if viewer_controller is not None:
            self.attach_viewer(viewer_controller)
        if project is not None:
            self.set_project(project)

    # -- reading ---------------------------------------------------------------

    @property
    def project(self) -> Any | None:
        """The current project (explicit, else the viewer controller's)."""
        if self._project is not None:
            return self._project
        if self._viewer_controller is not None:
            return getattr(self._viewer_controller, "project", None)
        return None

    @property
    def viewer_controller(self) -> Any | None:
        """The viewer controller this controller follows (if any)."""
        return self._viewer_controller

    @property
    def invoke_on_main(self) -> InvokeOnMain:
        """The marshalling callable (the app binds ``GLib.idle_add``)."""
        return self._invoke_on_main

    @property
    def observer(self) -> Observer:
        """The observer the airmass is computed at."""
        return self._observer

    @property
    def selected_session(self) -> str | None:
        """The explicitly selected session id (None = auto: the first)."""
        return self._session_id

    @property
    def listeners(self) -> tuple[QualityListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    @property
    def snapshot(self) -> QualitySnapshot:
        """The frozen :class:`QualitySnapshot` of the current state.

        Reads the project's sessions and the selected session's trends
        through :meth:`_call_store`. NEVER raises: no project yields an empty
        snapshot (``error=None``); a store failure yields ``error`` set.
        """
        project = self.project
        if project is None:
            return QualitySnapshot(sessions=(), session_id=None, trends=None, error=None)
        try:
            sessions = tuple(
                s.id for s in self._call_store(lambda: list_sessions(project))
            )
        except Exception as exc:  # surfaced as snapshot.error, never a crash  # noqa: BLE001
            logger.info("quality sessions read failed: %s", exc)
            return QualitySnapshot(
                sessions=(), session_id=None, trends=None,
                error=f"{type(exc).__name__}: {exc}",
            )
        session_id = self._session_id
        if session_id not in sessions:
            session_id = sessions[0] if sessions else None
        if session_id is None:
            return QualitySnapshot(sessions=sessions, session_id=None, trends=None, error=None)
        try:
            trends = self._call_store(
                lambda: session_trends(project, session_id, observer=self._observer)
            )
        except Exception as exc:  # surfaced as snapshot.error, never a crash  # noqa: BLE001
            logger.info("quality trends read failed: %s", exc)
            return QualitySnapshot(
                sessions=sessions, session_id=session_id, trends=None,
                error=f"{type(exc).__name__}: {exc}",
            )
        return QualitySnapshot(
            sessions=sessions, session_id=session_id, trends=trends, error=None
        )

    def refresh(self) -> QualitySnapshot:
        """Reload the sessions + trends from the store, notify the listeners,
        and return the fresh frozen :class:`QualitySnapshot`."""
        snapshot = self.snapshot
        self._emit(snapshot)
        return snapshot

    def sessions(self) -> tuple[str, ...]:
        """The project's session ids ordered by ``started_at`` (via
        :func:`seestar_controller.store.sessions.list_sessions`). Empty when
        no project is set; raises :class:`TrendsError` on a store failure."""
        project = self.project
        if project is None:
            return ()
        try:
            return tuple(
                s.id for s in self._call_store(lambda: list_sessions(project))
            )
        except Exception as exc:
            raise TrendsError(
                f"cannot list the sessions of project {getattr(project, 'slug', project)!r}: {exc}"
            ) from exc

    def select_session(self, session_id: str | None) -> QualitySnapshot:
        """Select the session the trends are shown for (None = auto: the
        first session), notify the listeners, and return the fresh snapshot.

        Raises :class:`TypeError` when ``session_id`` is not a string or None.
        An unknown id is NOT an error here — the snapshot falls back to the
        first session (the widget's dropdown only offers existing ids).
        """
        if session_id is not None and not isinstance(session_id, str):
            raise TypeError(f"session_id must be a string or None, got {session_id!r}")
        self._session_id = session_id
        return self.refresh()

    def set_observer(self, observer: Observer) -> QualitySnapshot:
        """Replace the :class:`Observer` the airmass is computed at, notify,
        and return the fresh snapshot (the airmass curve is recomputed)."""
        if not isinstance(observer, Observer):
            raise TypeError(f"observer must be an Observer, got {observer!r}")
        self._observer = observer
        return self.refresh()

    # -- listeners ---------------------------------------------------------------

    def add_listener(self, listener: QualityListener) -> QualityListener:
        """Register an observer callback (idempotent); returns it, so it can
        be used as a decorator. The listener receives a frozen
        :class:`QualitySnapshot` on any change (marshalled via
        ``invoke_on_main``)."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: QualityListener) -> None:
        """Unregister an observer callback (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self, snapshot: QualitySnapshot) -> None:
        """Hand a frozen snapshot to every listener; a failing listener is
        logged and isolated (never breaks the others or the caller)."""
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:
                logger.exception("quality listener failed")

    def _emit(self, snapshot: QualitySnapshot) -> None:
        """Marshal a listener notification through ``invoke_on_main``."""
        self._invoke_on_main(lambda: self._notify(snapshot))

    # -- project / viewer ----------------------------------------------------------

    def set_project(self, project: Any | None) -> None:
        """Set/replace the explicit project (None clears it — the viewer
        controller's project is then followed again). The db-owning thread is
        captured here (sqlite is thread-affine). Notifies."""
        self._project = project
        if project is not None:
            self._db_thread_id = threading.get_ident()
        self._emit(self.snapshot)

    def attach_viewer(self, viewer_controller: Any) -> None:
        """Follow ``viewer_controller`` (duck-typed — the shared viewer
        controller): register as its listener and adopt its project whenever
        no explicit project is set, so the quality trends follow the currently
        open project. Re-attaching detaches the previous viewer first."""
        if self._viewer_controller is not None:
            self._viewer_controller.remove_listener(self._on_viewer_snapshot)
        self._viewer_controller = viewer_controller
        viewer_controller.add_listener(self._on_viewer_snapshot)
        if self._project is None:
            # adopt the viewer's project (None -> empty snapshot) and notify
            if getattr(viewer_controller, "project", None) is not None:
                self._db_thread_id = threading.get_ident()
            self._emit(self.snapshot)

    def detach_viewer(self) -> None:
        """Stop following the viewer controller."""
        if self._viewer_controller is not None:
            self._viewer_controller.remove_listener(self._on_viewer_snapshot)
            self._viewer_controller = None

    def _on_viewer_snapshot(self, _snapshot: Any) -> None:
        """Viewer listener: adopt the viewer's project (when no explicit
        project is set) so the trends follow the open project — emits on every
        viewer change, so closing the project clears the view."""
        if self._project is not None or self._viewer_controller is None:
            return
        if getattr(self._viewer_controller, "project", None) is not None:
            self._db_thread_id = threading.get_ident()
        self._emit(self.snapshot)

    # -- store marshalling (sqlite is thread-affine) --------------------------------

    def _call_store(self, fn: Callable[[], Any]) -> Any:
        """Run the store callable ``fn`` and return its result.

        When already on the db-owning thread (or when no db thread was
        captured), ``fn`` runs directly. Otherwise it is marshalled through
        ``invoke_on_main`` (the app: ``GLib.idle_add``) and the calling thread
        waits for the result — the same pattern as
        :class:`seestar_controller.analysis.lightcurve.LightcurveController`.

        Before ``fn`` runs, the current project handle is ensured live
        (:meth:`~seestar_controller.store.project.Project.ensure_open`) when
        its db was closed (issue 08: re-opening a project used to leave
        controllers holding a stale, closed handle) — on the same thread
        ``fn`` runs on, so the sqlite thread-affinity is preserved."""
        project = self._project
        db_thread = self._db_thread_id
        if db_thread is None or db_thread == threading.get_ident():
            if project is not None:
                project.ensure_open()  # issue 08: revive a closed db handle
            return fn()
        box: dict[str, Any] = {}
        done = threading.Event()

        def wrapper() -> None:
            try:
                if project is not None:
                    project.ensure_open()  # issue 08: same, on the db thread
                box["result"] = fn()
            except BaseException as exc:  # re-raised on the calling thread  # noqa: BLE001
                box["error"] = exc
            finally:
                done.set()

        self._invoke_on_main(wrapper)
        if not done.wait(DB_CALL_TIMEOUT_S):
            raise TrendsError(
                "timed out waiting for the project db thread — is the main "
                "loop running?"
            )
        if "error" in box:
            raise box["error"]
        return box["result"]
