"""Lightcurve controller + plot transform math — the PURE layer of
period-analysis ticket 01 (:mod:`seestar_controller.analysis.lightcurve`).

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) — stdlib +
:mod:`seestar_controller.store` only. No :mod:`seestar_controller.link`, no
:mod:`ui`, no :mod:`photometry` imports. The widget layer
(:mod:`.lightcurve_view`) imports ``gi`` at module top and is never imported
by this module or eagerly by the package ``__init__``.

What lives here:

- :class:`AnalysisError` / :class:`LightcurveError` — the error hierarchy
  (both :class:`ValueError`\\ s);
- :class:`LightcurvePoint` — one frozen lightcurve point for plotting:
  ``measurement_id``, ``jd``, ``utc``, ``mag``, ``mag_err``, ``session_id``,
  ``excluded`` (mapped from the store's
  :class:`~seestar_controller.store.measurements.Measurement`);
- :class:`LightcurveSnapshot` — the frozen controller snapshot:
  ``points`` (ordered by jd, ALL sessions, excluded included),
  ``sessions`` (the session ids in first-seen order — the legend/color
  order), and ``error`` (the last read error message, else None);
- :class:`LightcurveController` — the app-level shared controller (the app
  owns one, following the shared viewer controller's project like the
  photometry controllers do): :attr:`~LightcurveController.snapshot` reads
  :func:`seestar_controller.store.measurements.lightcurve` with
  ``include_excluded=True``, :meth:`~LightcurveController.refresh` reloads
  and notifies, :meth:`~LightcurveController.set_project` /
  :meth:`~LightcurveController.attach_viewer` bind the current project, and
  listeners (:meth:`~LightcurveController.add_listener` /
  :meth:`~LightcurveController.remove_listener`) get a frozen
  :class:`LightcurveSnapshot` on every change, marshalled through
  ``invoke_on_main`` (the app binds ``GLib.idle_add``; the default calls
  directly, so tests are synchronous). The snapshot NEVER raises: no project
  or an empty project yields an empty snapshot, a store failure yields
  ``error`` set — the view renders whatever it gets. **Ticket 04** adds the
  outlier flagging: :meth:`~LightcurveController.set_excluded` /
  :meth:`~LightcurveController.toggle_excluded` write the store's
  ``excluded`` flag (through :meth:`_call_store`) and refresh, so the plot
  widgets' click-a-point gesture marks outliers and the periodogram (which
  reads the store lightcurve with its default ``include_excluded=False``)
  drops them;
- the **session color assignment**: :data:`SESSION_PALETTE` (a deterministic
  RGBA palette) with :func:`session_color_index` / :func:`session_color` —
  the palette index is the session's position in the snapshot's first-seen
  ``sessions`` order (cycling when there are more sessions than colors);
- the **plot transform math** (pure, unit-testable headless — the widget only
  feeds it events): :class:`PlotGeometry` (pixel size + margins),
  :class:`PlotWindow` (the visible data window ``jd_min``/``jd_max``/
  ``mag_min``/``mag_max`` — ``mag_min`` is the BRIGHTER end, drawn at the
  top), :func:`full_window` (the data range with padding, the reset view),
  :func:`data_to_screen` / :func:`screen_to_data` (the data<->screen mapping
  with the INVERTED mag axis — brighter at the top, matching
  :mod:`seestar_controller.photometry.results_view`), :func:`zoom_window_at`
  (zoom around the cursor — the data point under the cursor stays fixed),
  and :func:`pan_window` (shift the window by a pixel drag).

Conventions (documented choices):

- **Threading / sqlite thread-affinity.** Like
  :class:`seestar_controller.photometry.run.RunController`, every STORE call
  goes through :meth:`LightcurveController._call_store`: it runs directly on
  the db-owning thread (captured when the project is set) and is otherwise
  marshalled through ``invoke_on_main`` with a wait, so the controller is
  safe to call from a worker thread while every sqlite access stays on the
  db-owning thread.
- **Sessions in first-seen order.** The snapshot's ``sessions`` tuple is
  derived from the jd-ordered points (first occurrence wins), so the legend
  and the color assignment are stable and deterministic for a given
  lightcurve.
- **Zoom/pan math.** ``zoom_window_at(window, geom, x, y, factor)`` with
  ``factor > 1`` zooms IN (the window shrinks around the cursor's data
  point); ``pan_window(window, geom, dx, dy)`` shifts the window so the
  content follows the cursor (``dx``/``dy`` are pixel drag deltas). Both are
  pure functions returning a new frozen :class:`PlotWindow` — the widget
  stores the current window and redraws on change.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from seestar_controller.store.measurements import lightcurve
from seestar_controller.store.measurements import set_excluded as _store_set_excluded
from seestar_controller.store.sessions import list_sessions

logger = logging.getLogger(__name__)


class AnalysisError(ValueError):
    """Base class for period-analysis failures (a :class:`ValueError`)."""


class LightcurveError(AnalysisError):
    """A lightcurve-view failure: a store read/write failure while building
    the snapshot, or an invalid plot-window/geometry argument."""


#: How long :meth:`LightcurveController._call_store` waits for a marshalled
#: store call on the db-owning thread before giving up (the main loop must be
#: running). Same value as photometry's run controller (re-declared: this
#: package must not import photometry).
DB_CALL_TIMEOUT_S = 60.0


# --------------------------------------------------------------------------------------
# Points + snapshot
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LightcurvePoint:
    """One frozen lightcurve point for plotting (mapped from the store's
    :class:`~seestar_controller.store.measurements.Measurement`).

    - ``measurement_id`` — the db row id (outlier flagging target);
    - ``jd`` — the time of the point as Julian Date;
    - ``utc`` — the frame's observation time (UTC ISO-8601 string);
    - ``mag`` — the differential magnitude;
    - ``mag_err`` — the magnitude uncertainty, or None when unknown;
    - ``session_id`` — the night the point belongs to (drives its color);
    - ``excluded`` — the outlier flag (drawn marked, e.g. red).
    """

    measurement_id: int
    jd: float
    utc: str
    mag: float
    mag_err: float | None
    session_id: str
    excluded: bool


@dataclass(frozen=True)
class LightcurveSnapshot:
    """The frozen snapshot of a :class:`LightcurveController`, handed to the
    listeners on every change.

    - ``points`` — the lightcurve points (ALL sessions, excluded included,
      ordered by jd) as a tuple of :class:`LightcurvePoint`;
    - ``sessions`` — the session ids in first-seen order (the legend order
      and the color-assignment order);
    - ``error`` — the last store read error message, else None.
    """

    points: tuple[LightcurvePoint, ...]
    sessions: tuple[str, ...]
    error: str | None


#: A lightcurve listener: called with a frozen :class:`LightcurveSnapshot` on
#: any change (always marshalled via ``invoke_on_main``).
LightcurveListener = Callable[[LightcurveSnapshot], None]

#: Marshals a zero-argument callback onto the "main" thread. The widget layer
#: binds this to ``GLib.idle_add``; the default calls it directly.
InvokeOnMain = Callable[[Callable[[], None]], Any]


def _direct_invoke(callback: Callable[[], None]) -> None:
    """The default ``invoke_on_main``: run the callback directly (tests)."""
    callback()


def _point_from_measurement(m: Any) -> LightcurvePoint:
    """Map a store :class:`~seestar_controller.store.measurements.Measurement`
    to a frozen :class:`LightcurvePoint`."""
    return LightcurvePoint(
        measurement_id=m.id,
        jd=m.jd,
        utc=m.utc,
        mag=m.mag,
        mag_err=m.mag_err,
        session_id=m.session_id,
        excluded=m.excluded,
    )


def _sessions_first_seen(points: tuple[LightcurvePoint, ...]) -> tuple[str, ...]:
    """The session ids of ``points`` in first-seen order (points are jd
    ordered, so this is the chronological night order)."""
    seen: list[str] = []
    for point in points:
        if point.session_id not in seen:
            seen.append(point.session_id)
    return tuple(seen)


# --------------------------------------------------------------------------------------
# Session colors (deterministic palette assignment)
# --------------------------------------------------------------------------------------


#: The deterministic session palette (RGBA): one color per session, assigned
#: by the session's position in the snapshot's first-seen ``sessions`` order.
SESSION_PALETTE: tuple[tuple[float, float, float, float], ...] = (
    (0.35, 0.60, 0.95, 1.0),  # blue
    (0.95, 0.62, 0.25, 1.0),  # orange
    (0.45, 0.85, 0.45, 1.0),  # green
    (0.85, 0.50, 0.90, 1.0),  # purple
    (0.95, 0.85, 0.30, 1.0),  # yellow
    (0.40, 0.90, 0.90, 1.0),  # cyan
    (0.95, 0.45, 0.55, 1.0),  # red
    (0.75, 0.75, 0.80, 1.0),  # gray
)

#: The color of excluded (outlier) points — red, matching the photometry
#: results view's convention.
EXCLUDED_COLOR: tuple[float, float, float, float] = (0.90, 0.35, 0.30, 1.0)

#: The axis/tick/text color (gray).
AXIS_COLOR: tuple[float, float, float, float] = (0.55, 0.55, 0.58, 1.0)


def session_color_index(sessions: Any, session_id: str) -> int:
    """The deterministic palette index of ``session_id`` given the snapshot's
    ``sessions`` (first-seen order): its position in ``sessions``, or
    ``len(sessions)`` when unknown (which cycles to the palette start when
    there are more sessions than colors)."""
    sessions = tuple(sessions)
    try:
        return sessions.index(session_id)
    except ValueError:
        return len(sessions)


def session_color(sessions: Any, session_id: str) -> tuple[float, float, float, float]:
    """The RGBA color of ``session_id``: the palette entry at
    :func:`session_color_index`, cycling when there are more sessions than
    palette colors."""
    index = session_color_index(sessions, session_id)
    return SESSION_PALETTE[index % len(SESSION_PALETTE)]


# --------------------------------------------------------------------------------------
# Plot transform math (pure — the widget only feeds it events)
# --------------------------------------------------------------------------------------

#: Plot margins (pixels): left (mag axis labels), right, top, bottom (jd ticks).
PLOT_MARGIN_LEFT = 64
PLOT_MARGIN_RIGHT = 12
PLOT_MARGIN_TOP = 12
PLOT_MARGIN_BOTTOM = 30

#: The zoom factor per scroll step (``ZOOM_FACTOR ** steps``; scrolling up
#: zooms in).
ZOOM_FACTOR = 1.25

#: Minimum window spans (zoom-in clamp): ~0.1 s in JD, 1 mmag.
MIN_JD_SPAN = 1e-9
MIN_MAG_SPAN = 1e-6

#: The default window when there are no points (the placeholder is shown
#: then, but reset/zoom must still not crash) — assigned after PlotWindow.
DEFAULT_WINDOW: PlotWindow


@dataclass(frozen=True)
class PlotGeometry:
    """The plot's pixel geometry: the drawing area size plus the margins.

    ``plot_width`` / ``plot_height`` are the inner plot rectangle (at least
    1 px, so the transforms never divide by zero).
    """

    width: int
    height: int
    margin_left: int = PLOT_MARGIN_LEFT
    margin_right: int = PLOT_MARGIN_RIGHT
    margin_top: int = PLOT_MARGIN_TOP
    margin_bottom: int = PLOT_MARGIN_BOTTOM

    def __post_init__(self) -> None:
        for name in ("width", "height", "margin_left", "margin_right", "margin_top", "margin_bottom"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise LightcurveError(f"{name} must be a non-negative int, got {value!r}")

    @property
    def plot_width(self) -> int:
        """The inner plot width in pixels (at least 1)."""
        return max(self.width - self.margin_left - self.margin_right, 1)

    @property
    def plot_height(self) -> int:
        """The inner plot height in pixels (at least 1)."""
        return max(self.height - self.margin_top - self.margin_bottom, 1)


@dataclass(frozen=True)
class PlotWindow:
    """The visible data window: ``jd_min``/``jd_max`` on the x axis and
    ``mag_min``/``mag_max`` on the y axis.

    ``mag_min`` is the BRIGHTER end (smaller magnitude) and is drawn at the
    TOP — the mag axis is inverted (brighter up), matching the photometry
    results view. ``jd_max > jd_min`` and ``mag_max > mag_min`` are enforced.
    """

    jd_min: float
    jd_max: float
    mag_min: float
    mag_max: float

    def __post_init__(self) -> None:
        for name in ("jd_min", "jd_max", "mag_min", "mag_max"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise LightcurveError(f"{name} must be a number, got {value!r}")
            if not math.isfinite(float(value)):
                raise LightcurveError(f"{name} must be finite, got {value!r}")
        if not self.jd_max > self.jd_min:
            raise LightcurveError(
                f"jd_max must be > jd_min, got [{self.jd_min}, {self.jd_max}]"
            )
        if not self.mag_max > self.mag_min:
            raise LightcurveError(
                f"mag_max must be > mag_min, got [{self.mag_min}, {self.mag_max}]"
            )

    @property
    def jd_span(self) -> float:
        """The visible JD range."""
        return self.jd_max - self.jd_min

    @property
    def mag_span(self) -> float:
        """The visible magnitude range."""
        return self.mag_max - self.mag_min


DEFAULT_WINDOW = PlotWindow(jd_min=0.0, jd_max=1.0, mag_min=-1.0, mag_max=1.0)


def full_window(points: Any) -> PlotWindow:
    """The full data window of ``points`` (the reset view): the jd range of
    the points (degenerate range expanded by ±1e-6 d) and the mag range
    padded by 5% on each side (degenerate range expanded by ±0.05 mag).

    Empty ``points`` yield :data:`DEFAULT_WINDOW`.
    """
    points = tuple(points)
    if not points:
        return DEFAULT_WINDOW
    jds = [p.jd for p in points]
    mags = [p.mag for p in points]
    jd_min, jd_max = min(jds), max(jds)
    if jd_max - jd_min < 1e-9:
        jd_min, jd_max = jd_min - 1e-6, jd_max + 1e-6
    mag_min, mag_max = min(mags), max(mags)
    if mag_max - mag_min < 1e-6:
        mag_min, mag_max = mag_min - 0.05, mag_max + 0.05
    else:
        pad = 0.05 * (mag_max - mag_min)
        mag_min, mag_max = mag_min - pad, mag_max + pad
    return PlotWindow(jd_min=jd_min, jd_max=jd_max, mag_min=mag_min, mag_max=mag_max)


def data_to_screen(
    window: PlotWindow, geom: PlotGeometry, jd: float, mag: float
) -> tuple[float, float]:
    """Map a data point ``(jd, mag)`` to plot pixel coordinates ``(x, y)``.

    The mag axis is INVERTED: brighter (smaller mag) maps to a smaller ``y``
    (the top of the plot) — ``window.mag_min`` is drawn at
    ``geom.margin_top``.
    """
    x = geom.margin_left + (jd - window.jd_min) / window.jd_span * geom.plot_width
    y = geom.margin_top + (mag - window.mag_min) / window.mag_span * geom.plot_height
    return x, y


def screen_to_data(
    window: PlotWindow, geom: PlotGeometry, x: float, y: float
) -> tuple[float, float]:
    """Map plot pixel coordinates ``(x, y)`` back to data ``(jd, mag)`` —
    the exact inverse of :func:`data_to_screen`."""
    jd = window.jd_min + (x - geom.margin_left) / geom.plot_width * window.jd_span
    mag = window.mag_min + (y - geom.margin_top) / geom.plot_height * window.mag_span
    return jd, mag


def _clamp_span(minimum: float, center: float, span: float) -> tuple[float, float]:
    """Clamp a window half-range to ``minimum`` span, centered on ``center``:
    returns ``(lo, hi)`` with ``hi - lo >= minimum``."""
    if span < minimum:
        return center - minimum / 2.0, center + minimum / 2.0
    half = span / 2.0
    return center - half, center + half


def zoom_window_at(
    window: PlotWindow, geom: PlotGeometry, x: float, y: float, factor: float
) -> PlotWindow:
    """Zoom ``window`` around the cursor at pixel ``(x, y)`` by ``factor``
    (``factor > 1`` zooms IN — the window shrinks; ``factor < 1`` zooms out).

    The data point under the cursor STAYS under the cursor (the zoom is
    centered on it). The spans are clamped to :data:`MIN_JD_SPAN` /
    :data:`MIN_MAG_SPAN` so zooming in never degenerates the window.
    """
    if isinstance(factor, bool) or not isinstance(factor, (int, float)):
        raise LightcurveError(f"factor must be a number, got {factor!r}")
    factor = float(factor)
    if not math.isfinite(factor) or factor <= 0.0:
        raise LightcurveError(f"factor must be a finite number > 0, got {factor!r}")
    jd_c, mag_c = screen_to_data(window, geom, x, y)
    jd_lo, jd_hi = _clamp_span(
        MIN_JD_SPAN, jd_c, window.jd_span / factor
    )
    # keep the cursor's data point fixed: its relative position is preserved
    jd_min = jd_c - (jd_c - window.jd_min) / factor
    jd_max = jd_c + (window.jd_max - jd_c) / factor
    if jd_max - jd_min < MIN_JD_SPAN:
        jd_min, jd_max = jd_lo, jd_hi
    mag_min = mag_c - (mag_c - window.mag_min) / factor
    mag_max = mag_c + (window.mag_max - mag_c) / factor
    if mag_max - mag_min < MIN_MAG_SPAN:
        mag_min, mag_max = _clamp_span(MIN_MAG_SPAN, mag_c, window.mag_span / factor)
    return PlotWindow(jd_min=jd_min, jd_max=jd_max, mag_min=mag_min, mag_max=mag_max)


def pan_window(window: PlotWindow, geom: PlotGeometry, dx: float, dy: float) -> PlotWindow:
    """Pan ``window`` by a pixel drag ``(dx, dy)``: the content follows the
    cursor, so the window shifts by the opposite data delta.

    - ``dx > 0`` (dragged right) shifts the window toward smaller jd;
    - ``dy > 0`` (dragged down) shifts the window toward smaller mag
      (brighter — the mag axis is inverted, brighter at the top).
    """
    for name, value in (("dx", dx), ("dy", dy)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise LightcurveError(f"{name} must be a number, got {value!r}")
        if not math.isfinite(float(value)):
            raise LightcurveError(f"{name} must be finite, got {value!r}")
    jd_shift = -float(dx) / geom.plot_width * window.jd_span
    mag_shift = -float(dy) / geom.plot_height * window.mag_span
    return PlotWindow(
        jd_min=window.jd_min + jd_shift,
        jd_max=window.jd_max + jd_shift,
        mag_min=window.mag_min + mag_shift,
        mag_max=window.mag_max + mag_shift,
    )


# --------------------------------------------------------------------------------------
# The controller
# --------------------------------------------------------------------------------------


class LightcurveController:
    """The app-level lightcurve controller (period-analysis ticket 01).

    - ``project`` — the store project to read (None = follow the attached
      viewer controller's project, see :meth:`attach_viewer`);
    - ``viewer_controller`` — an optional duck-typed viewer controller (the
      shared :class:`~seestar_controller.enhance.viewer_controller.ViewerController`
      — NOT imported here): its project is adopted whenever no explicit
      project is set, so the lightcurve follows the currently open project;
    - ``invoke_on_main`` — the marshalling callable for listener notifications
      AND for store calls made from a non-db thread (the app binds
      ``GLib.idle_add``; the default calls directly).

    Operations: :attr:`snapshot` (the frozen :class:`LightcurveSnapshot` —
    never raises), :meth:`refresh` (reload + notify), :meth:`set_project`,
    :meth:`attach_viewer` / :meth:`detach_viewer`, :meth:`sessions` (the
    project's sessions via the store), and listeners with frozen snapshots.
    """

    def __init__(
        self,
        project: Any | None = None,
        viewer_controller: Any | None = None,
        *,
        invoke_on_main: InvokeOnMain | None = None,
    ) -> None:
        self._project: Any | None = None
        self._viewer_controller: Any | None = None
        self._invoke_on_main: InvokeOnMain = invoke_on_main or _direct_invoke
        self._lock = threading.Lock()
        self._listeners: list[LightcurveListener] = []
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
    def listeners(self) -> tuple[LightcurveListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    @property
    def snapshot(self) -> LightcurveSnapshot:
        """The frozen :class:`LightcurveSnapshot` of the current state.

        Reads the project's lightcurve (``include_excluded=True``, ordered
        by jd) through :meth:`_call_store`. NEVER raises: no project or an
        empty project yields an empty snapshot (``error=None``); a store
        failure yields ``error`` set with empty points.
        """
        project = self.project
        if project is None:
            return LightcurveSnapshot(points=(), sessions=(), error=None)
        try:
            measurements = self._call_store(
                lambda: lightcurve(project, include_excluded=True)
            )
        except Exception as exc:  # surfaced as snapshot.error, never a crash  # noqa: BLE001
            logger.info("lightcurve read failed: %s", exc)
            return LightcurveSnapshot(points=(), sessions=(), error=f"{type(exc).__name__}: {exc}")
        points = tuple(_point_from_measurement(m) for m in measurements)
        return LightcurveSnapshot(
            points=points, sessions=_sessions_first_seen(points), error=None
        )

    def refresh(self) -> LightcurveSnapshot:
        """Reload the lightcurve from the store, notify the listeners, and
        return the fresh frozen :class:`LightcurveSnapshot`."""
        snapshot = self.snapshot
        self._emit(snapshot)
        return snapshot

    # -- outlier flagging (period-analysis ticket 04) ---------------------------------

    def set_excluded(self, measurement_id: int, excluded: bool) -> LightcurvePoint:
        """Flag (``excluded=True``) or unflag (``excluded=False``) the outlier
        point ``measurement_id`` in the current project — the store write
        goes through :meth:`_call_store` (sqlite thread-affinity) — then
        reload + notify (the views re-render, the point drawn marked) and
        return the updated point as a frozen :class:`LightcurvePoint`.

        Raises :class:`LightcurveError` when there is no project, the id is
        not an int, ``excluded`` is not a bool, or the store call fails
        (e.g. an unknown measurement id — wrapped from
        :class:`~seestar_controller.store.errors.MeasurementError`).
        """
        if isinstance(measurement_id, bool) or not isinstance(measurement_id, int):
            raise LightcurveError(f"measurement_id must be an int, got {measurement_id!r}")
        if not isinstance(excluded, bool):
            raise LightcurveError(f"excluded must be a bool, got {excluded!r}")
        project = self.project
        if project is None:
            raise LightcurveError("no project — cannot flag an outlier point")
        try:
            updated = self._call_store(
                lambda: _store_set_excluded(project, measurement_id, excluded)
            )
        except Exception as exc:
            raise LightcurveError(
                f"cannot set the outlier flag of measurement {measurement_id} in "
                f"project {getattr(project, 'slug', project)!r}: {exc}"
            ) from exc
        self.refresh()  # re-read + notify: the views re-render the flagged point
        return _point_from_measurement(updated)

    def toggle_excluded(self, measurement_id: int) -> bool:
        """Toggle the outlier flag of ``measurement_id`` (the click-a-point
        gesture of the plot widgets calls this) and return the NEW
        ``excluded`` state.

        Raises :class:`LightcurveError` when the measurement is not in the
        current lightcurve (or no project — see :meth:`set_excluded`).
        """
        current = next(
            (p for p in self.snapshot.points if p.measurement_id == measurement_id),
            None,
        )
        if current is None:
            raise LightcurveError(
                f"no measurement {measurement_id!r} in the current lightcurve"
            )
        new_state = not current.excluded
        self.set_excluded(measurement_id, new_state)
        return new_state

    def sessions(self) -> tuple[str, ...]:
        """The project's session ids ordered by ``started_at`` (via
        :func:`seestar_controller.store.sessions.list_sessions`). Empty when
        no project is set; raises :class:`LightcurveError` on a store
        failure."""
        project = self.project
        if project is None:
            return ()
        try:
            return tuple(
                s.id for s in self._call_store(lambda: list_sessions(project))
            )
        except Exception as exc:
            raise LightcurveError(
                f"cannot list the sessions of project {getattr(project, 'slug', project)!r}: {exc}"
            ) from exc

    # -- listeners ---------------------------------------------------------------

    def add_listener(self, listener: LightcurveListener) -> LightcurveListener:
        """Register an observer callback (idempotent); returns it, so it can
        be used as a decorator. The listener receives a frozen
        :class:`LightcurveSnapshot` on any change (marshalled via
        ``invoke_on_main``)."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: LightcurveListener) -> None:
        """Unregister an observer callback (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self, snapshot: LightcurveSnapshot) -> None:
        """Hand a frozen snapshot to every listener; a failing listener is
        logged and isolated (never breaks the others or the caller)."""
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:
                logger.exception("lightcurve listener failed")

    def _emit(self, snapshot: LightcurveSnapshot) -> None:
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
        no explicit project is set, so the lightcurve follows the currently
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
        project is set) so the lightcurve follows the open project — emits
        on every viewer change, so closing the project clears the plot."""
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
        :class:`seestar_controller.photometry.run.RunController`.

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
            raise LightcurveError(
                "timed out waiting for the project db thread — is the main "
                "loop running?"
            )
        if "error" in box:
            raise box["error"]
        return box["result"]
