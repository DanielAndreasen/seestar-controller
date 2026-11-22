"""Pure-layer tests for period-analysis ticket 01 — the lightcurve view's
controller + plot transform math
(:mod:`seestar_controller.analysis.lightcurve`).

Headless: no ``gi`` anywhere (the sandbox has no GTK, see
docs/wiki/assumptions.md). The controller is exercised against a tmp project
with measurements across TWO sessions (built with the same store helpers as
tests/test_photometry_run.py: ``start_session`` / ``store_frame`` /
``add_measurement`` / ``close_session``).

Covered: the frozen :class:`LightcurveSnapshot` (points ordered by jd across
all sessions, excluded included and flagged, the first-seen ``sessions``
order, ``measurement_id``/``mag_err``/``utc`` carried, the never-raising
``error`` — empty project and no project yield an empty snapshot, a store
failure yields ``error`` set), ``refresh()`` + the listener pattern
(add/remove, idempotent, failing listener isolated, notifications marshalled
through ``invoke_on_main``), ``set_project`` / ``attach_viewer`` (the
lightcurve follows the viewer controller's project), ``sessions()``, the
worker-thread safety (store calls marshalled through ``invoke_on_main`` to
the db-owning thread), the deterministic session color assignment
(:data:`SESSION_PALETTE` cycling), the plot transform math
(:class:`PlotGeometry` / :class:`PlotWindow` validation, :func:`full_window`
padding + degenerate ranges, the data<->screen round trip, the INVERTED mag
axis, zoom-at-cursor keeping the cursor's data point fixed, pan shifting the
window so the content follows the cursor, the zoom clamps), and the package
boundary (subprocess + source-scan, mirroring tests/test_photometry_picking.py).
"""

import io
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits as pyfits

from seestar_controller import analysis
from seestar_controller.analysis import lightcurve as lightcurve_module
from seestar_controller.analysis.lightcurve import (
    AXIS_COLOR,
    DB_CALL_TIMEOUT_S,
    DEFAULT_WINDOW,
    EXCLUDED_COLOR,
    MIN_JD_SPAN,
    MIN_MAG_SPAN,
    SESSION_PALETTE,
    ZOOM_FACTOR,
    AnalysisError,
    LightcurveController,
    LightcurveError,
    LightcurvePoint,
    LightcurveSnapshot,
    PlotGeometry,
    PlotWindow,
    data_to_screen,
    full_window,
    pan_window,
    screen_to_data,
    session_color,
    session_color_index,
    zoom_window_at,
)
from seestar_controller.store import (
    KIND_SUB,
    add_measurement,
    close_session,
    create_project,
    lightcurve,
    list_sessions,
    start_session,
    store_frame,
)

#: Directory of the analysis package (source-scan audit).
ANALYSIS_PACKAGE_DIR = Path(analysis.__file__).resolve().parent

#: The pure analysis modules — no ``gi`` import allowed (subprocess-enforced).
PURE_ANALYSIS_MODULES = ("lightcurve", "periodogram", "fold", "export")
#: The widget analysis modules — ``import gi`` at module top, lazy export.
WIDGET_ANALYSIS_MODULES = ("lightcurve_view", "period_view")

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)
JD0 = 2460000.5


def frozen(dt: datetime):
    return lambda: dt


def fits_bytes(date_obs: str) -> bytes:
    """A small valid FITS file with the given DATE-OBS (drives the frame jd)."""
    hdu = pyfits.PrimaryHDU(data=np.zeros((8, 8), dtype=np.float32))
    hdu.header["DATE-OBS"] = date_obs
    hdu.header["EXPTIME"] = 10.0
    hdu.header["OBJECT"] = "M 42"
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def date_obs_str(offset_seconds: float) -> str:
    instant = T0 + timedelta(seconds=offset_seconds)
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "")


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    p = create_project("M 42 night", root=root)
    yield p
    p.close()


def add_session_points(
    project,
    *,
    clock: datetime,
    jds: list[float],
    mags: list[float],
    mag_errs: list[float | None] | None = None,
    excluded: list[bool] | None = None,
) -> str:
    """One session with one sub-frame + one measurement per (jd, mag) pair.
    Returns the session id. Any still-open session is closed first (the
    store allows only one open session at a time)."""
    for open_session in list_sessions(project):
        if open_session.ended_at is None:
            close_session(project, open_session.id)
    session_id = start_session(project, clock=frozen(clock))
    mag_errs = mag_errs if mag_errs is not None else [None] * len(jds)
    excluded = excluded if excluded is not None else [False] * len(jds)
    for i, (jd, mag) in enumerate(zip(jds, mags)):
        frame = store_frame(
            project,
            session_id,
            KIND_SUB,
            fits_bytes(date_obs_str(10.0 * i)),
        )
        add_measurement(
            project,
            frame.id,
            mag,
            jd=jd,
            mag_err=mag_errs[i],
            excluded=excluded[i],
        )
    close_session(project, session_id)
    return session_id


def two_session_project(project) -> tuple[str, str]:
    """Two sessions with interleaved jds; session A has one excluded point.
    Returns ``(session_a, session_b)``."""
    session_a = add_session_points(
        project,
        clock=T0,
        jds=[JD0 + 0.10, JD0 + 0.20, JD0 + 0.30],
        mags=[12.10, 12.40, 12.20],
        mag_errs=[0.02, None, 0.03],
        excluded=[False, True, False],
    )
    session_b = add_session_points(
        project,
        clock=T0 + timedelta(days=1),
        jds=[JD0 + 0.15, JD0 + 0.25],
        mags=[12.30, 12.50],
    )
    return session_a, session_b


class QueuedInvoke:
    """An ``invoke_on_main`` that queues callbacks instead of running them —
    the test pumps the queue from the "main" thread (mirrors GLib.idle_add)."""

    def __init__(self) -> None:
        self.callbacks: queue.Queue = queue.Queue()

    def __call__(self, callback) -> int:
        self.callbacks.put(callback)
        return 1

    def pump_one(self, timeout: float = 0.05) -> bool:
        try:
            callback = self.callbacks.get(timeout=timeout)
        except queue.Empty:
            return False
        callback()
        return True

    def drain(self) -> int:
        n = 0
        while self.pump_one(timeout=0.0):
            n += 1
        return n


class FakeViewerController:
    """A duck-typed stand-in for the shared viewer controller: a ``project``
    attribute plus the listener protocol."""

    def __init__(self, project=None) -> None:
        self.project = project
        self._listeners: list = []

    def add_listener(self, listener) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_listener(self, listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def set_project(self, project) -> None:
        self.project = project
        for listener in list(self._listeners):
            listener(None)


# --- snapshot: empty / no project / two sessions -----------------------------------------------------


def test_snapshot_of_empty_project_is_empty_not_a_crash(project):
    ctrl = LightcurveController(project)
    snap = ctrl.snapshot
    assert isinstance(snap, LightcurveSnapshot)
    assert snap.points == ()
    assert snap.sessions == ()
    assert snap.error is None


def test_snapshot_without_project_is_empty():
    ctrl = LightcurveController()
    assert ctrl.project is None
    snap = ctrl.snapshot
    assert snap.points == () and snap.sessions == () and snap.error is None
    assert ctrl.sessions() == ()


def test_snapshot_two_sessions_ordered_by_jd_first_seen_excluded_flagged(project):
    session_a, session_b = two_session_project(project)
    ctrl = LightcurveController(project)
    snap = ctrl.snapshot
    assert snap.error is None
    # ordered by jd across BOTH sessions (interleaved)
    assert [p.jd for p in snap.points] == sorted(p.jd for p in snap.points)
    assert len(snap.points) == 5
    # sessions in first-seen order (session A's first point is the earliest)
    assert snap.sessions == (session_a, session_b)
    # the store's measurement ids / mags / errors / excluded flags are carried
    store_points = lightcurve(project, include_excluded=True)
    assert [p.measurement_id for p in snap.points] == [m.id for m in store_points]
    by_id = {m.id: m for m in store_points}
    for point in snap.points:
        m = by_id[point.measurement_id]
        assert point.mag == m.mag
        assert point.mag_err == m.mag_err
        assert point.utc == m.utc
        assert point.session_id == m.session_id
        assert point.excluded == m.excluded
    excluded_points = [p for p in snap.points if p.excluded]
    assert len(excluded_points) == 1
    assert excluded_points[0].session_id == session_a
    assert excluded_points[0].mag == 12.40
    assert excluded_points[0].mag_err is None
    # points are frozen
    with pytest.raises(Exception):  # noqa: B017
        snap.points[0].mag = 99.0  # type: ignore[misc]  # ty: ignore[invalid-assignment]


def test_snapshot_includes_excluded_points(project):
    """The controller reads with ``include_excluded=True`` (unlike the store
    default) — excluded points are in the snapshot, flagged."""
    _session_a, _ = two_session_project(project)
    ctrl = LightcurveController(project)
    snap = ctrl.snapshot
    assert any(p.excluded for p in snap.points)
    assert len(snap.points) == len(lightcurve(project, include_excluded=True))
    assert len(snap.points) > len(lightcurve(project))  # store default filters


# --- refresh + listeners ---------------------------------------------------------------------------------


def test_refresh_reloads_and_notifies_listeners(project):
    session_a, _ = two_session_project(project)
    ctrl = LightcurveController(project)
    seen: list[LightcurveSnapshot] = []
    ctrl.add_listener(seen.append)
    snap = ctrl.refresh()
    assert len(snap.points) == 5
    assert seen == [snap]  # direct invoke_on_main: synchronous
    # add one more point, refresh again -> the listener sees the new snapshot
    add_session_points(
        project, clock=T0 + timedelta(days=2), jds=[JD0 + 0.40], mags=[12.60]
    )
    snap2 = ctrl.refresh()
    assert len(snap2.points) == 6
    assert seen[-1] is snap2
    assert snap2.sessions[-1] != session_a


def test_add_remove_listener_idempotent(project):
    ctrl = LightcurveController(project)
    seen: list[LightcurveSnapshot] = []
    ctrl.add_listener(seen.append)
    ctrl.add_listener(seen.append)  # idempotent
    assert ctrl.listeners == (seen.append,)
    ctrl.refresh()
    assert len(seen) == 1
    ctrl.remove_listener(seen.append)
    ctrl.remove_listener(seen.append)  # no-op
    assert ctrl.listeners == ()
    ctrl.refresh()
    assert len(seen) == 1  # no longer notified


def test_failing_listener_is_isolated(project):
    ctrl = LightcurveController(project)
    seen: list[LightcurveSnapshot] = []

    def bad(_snapshot):
        raise RuntimeError("boom")

    ctrl.add_listener(bad)
    ctrl.add_listener(seen.append)
    snap = ctrl.refresh()  # must not raise
    assert seen == [snap]


def test_queued_invoke_marshals_listener_notifications(project):
    two_session_project(project)
    queued = QueuedInvoke()
    ctrl = LightcurveController(project, invoke_on_main=queued)
    queued.drain()  # the constructor's set_project notification
    seen: list[LightcurveSnapshot] = []
    ctrl.add_listener(seen.append)
    ctrl.refresh()  # same thread: the store read runs directly...
    assert seen == []  # ...but the notification is queued
    queued.drain()
    assert len(seen) == 1
    assert len(seen[0].points) == 5


# --- project / viewer binding ------------------------------------------------------------------------------


def test_set_project_switches_and_notifies(project, root):
    _session_a, _ = two_session_project(project)
    other = create_project("M 31 night", root=root)
    try:
        ctrl = LightcurveController(project)
        seen: list[LightcurveSnapshot] = []
        ctrl.add_listener(seen.append)
        assert len(ctrl.snapshot.points) == 5
        ctrl.set_project(other)
        assert ctrl.project is other
        assert ctrl.snapshot.points == ()  # the other project is empty
        assert seen[-1].points == ()
        ctrl.set_project(None)  # clears -> no project -> empty snapshot
        assert ctrl.project is None
        assert ctrl.snapshot.points == ()
        assert len(seen) == 2  # set_project(other) + set_project(None)
    finally:
        other.close()


def test_attach_viewer_follows_the_viewers_project(project):
    _session_a, _ = two_session_project(project)
    viewer = FakeViewerController(project)
    ctrl = LightcurveController(viewer_controller=viewer)
    assert ctrl.viewer_controller is viewer
    assert ctrl.project is project  # adopted from the viewer
    assert len(ctrl.snapshot.points) == 5
    # the viewer switching projects re-emits the lightcurve snapshot
    seen: list[LightcurveSnapshot] = []
    ctrl.add_listener(seen.append)
    viewer.set_project(None)
    assert ctrl.project is None
    assert seen[-1].points == ()
    # an explicit project wins over the viewer's
    ctrl.set_project(project)
    viewer.set_project(None)
    assert ctrl.project is project
    assert len(ctrl.snapshot.points) == 5
    # detach stops following
    ctrl.detach_viewer()
    assert ctrl.viewer_controller is None
    viewer.set_project(project)
    assert ctrl.project is project  # still the explicit one


def test_sessions_lists_the_project_sessions_in_started_order(project):
    session_a, session_b = two_session_project(project)
    ctrl = LightcurveController(project)
    assert ctrl.sessions() == (session_a, session_b)
    assert ctrl.sessions() == tuple(s.id for s in list_sessions(project))


# --- store failure -> error snapshot (never raises) ---------------------------------------------------------


def test_store_failure_yields_error_snapshot_not_a_crash(project, monkeypatch):
    two_session_project(project)
    ctrl = LightcurveController(project)
    assert ctrl.snapshot.error is None

    def boom(*args, **kwargs):
        raise RuntimeError("injected store failure")

    # issue 08: a closed db handle is revived by ensure_open(), so the failure
    # is injected at the store function instead of via project.close()
    monkeypatch.setattr(lightcurve_module, "lightcurve", boom)
    monkeypatch.setattr(lightcurve_module, "list_sessions", boom)
    snap = ctrl.snapshot
    assert snap.points == ()
    assert snap.sessions == ()
    assert snap.error is not None
    assert "Error" in snap.error
    with pytest.raises(LightcurveError):
        ctrl.sessions()


# --- worker-thread safety: store calls marshalled to the db-owning thread -----------------------------------


def test_snapshot_from_a_worker_thread_marshals_store_calls(project):
    """The project db lives on the main thread; ``snapshot`` called from a
    worker thread marshals the store read through ``invoke_on_main`` (pumped
    by the main thread here, GLib.idle_add in the app)."""
    two_session_project(project)
    queued = QueuedInvoke()
    ctrl = LightcurveController(project, invoke_on_main=queued)
    result: dict = {}

    def work():
        result["snapshot"] = ctrl.snapshot

    worker = threading.Thread(target=work, name="test-lightcurve-worker", daemon=True)
    worker.start()
    main_id = threading.get_ident()
    deadline = time.monotonic() + DB_CALL_TIMEOUT_S
    while worker.is_alive() and time.monotonic() < deadline:
        queued.pump_one(timeout=0.05)  # the "main loop" runs the store read
    worker.join(timeout=10.0)
    assert not worker.is_alive()
    assert worker.ident != main_id
    snap = result["snapshot"]
    assert snap.error is None
    assert len(snap.points) == 5
    assert set(snap.sessions) == {p.session_id for p in snap.points}


def test_snapshot_on_the_db_owning_thread_runs_directly(project):
    """No marshalling needed when the caller IS the db-owning thread."""
    two_session_project(project)
    queued = QueuedInvoke()
    ctrl = LightcurveController(project, invoke_on_main=queued)
    queued.drain()  # the constructor's set_project notification
    snap = ctrl.snapshot  # main thread owns the db (set_project captured it)
    assert len(snap.points) == 5
    assert queued.drain() == 0  # nothing was marshalled


# --- session color assignment (deterministic palette) --------------------------------------------------------


def test_session_color_assignment_is_deterministic_and_cycles():
    sessions = ("2026-11-20", "2026-11-21", "2026-11-22")
    assert session_color_index(sessions, "2026-11-20") == 0
    assert session_color_index(sessions, "2026-11-21") == 1
    assert session_color_index(sessions, "2026-11-22") == 2
    # unknown session -> len(sessions) (cycles to the palette start)
    assert session_color_index(sessions, "unknown") == 3
    assert session_color(sessions, "2026-11-20") == SESSION_PALETTE[0]
    assert session_color(sessions, "2026-11-21") == SESSION_PALETTE[1]
    assert session_color(sessions, "unknown") == SESSION_PALETTE[3]
    # more sessions than palette colors -> cycles
    many = tuple(f"s{i}" for i in range(len(SESSION_PALETTE) + 2))
    assert session_color(many, f"s{len(SESSION_PALETTE)}") == SESSION_PALETTE[0]
    assert session_color(many, "s1") == SESSION_PALETTE[1]
    # deterministic: same input -> same color
    assert session_color(sessions, "2026-11-20") == session_color(sessions, "2026-11-20")
    # palette entries are RGBA tuples
    for color in SESSION_PALETTE:
        assert len(color) == 4 and all(0.0 <= c <= 1.0 for c in color)
    assert EXCLUDED_COLOR != SESSION_PALETTE[0]
    assert len(AXIS_COLOR) == 4


def test_snapshot_sessions_drive_the_color_order(project):
    session_a, session_b = two_session_project(project)
    snap = LightcurveController(project).snapshot
    assert session_color(snap.sessions, session_a) == SESSION_PALETTE[0]
    assert session_color(snap.sessions, session_b) == SESSION_PALETTE[1]


# --- plot transform math -------------------------------------------------------------------------------------


GEOM = PlotGeometry(480, 320)
# small jd values keep the float precision of the transform tests high
# (a jd of ~2.46e6 has an eps of ~5e-10, which would blur pixel assertions)
WINDOW = PlotWindow(jd_min=0.0, jd_max=0.5, mag_min=12.0, mag_max=13.0)


def test_plot_window_validation():
    with pytest.raises(LightcurveError):
        PlotWindow(jd_min=1.0, jd_max=1.0, mag_min=0.0, mag_max=1.0)
    with pytest.raises(LightcurveError):
        PlotWindow(jd_min=2.0, jd_max=1.0, mag_min=0.0, mag_max=1.0)
    with pytest.raises(LightcurveError):
        PlotWindow(jd_min=0.0, jd_max=1.0, mag_min=2.0, mag_max=1.0)
    with pytest.raises(LightcurveError):
        PlotWindow(jd_min=0.0, jd_max=float("nan"), mag_min=0.0, mag_max=1.0)
    with pytest.raises(LightcurveError):
        PlotWindow(jd_min=0.0, jd_max=1.0, mag_min=0.0, mag_max=float("inf"))
    with pytest.raises(LightcurveError):
        PlotWindow(jd_min=True, jd_max=1.0, mag_min=0.0, mag_max=1.0)
    w = PlotWindow(jd_min=0.0, jd_max=2.0, mag_min=1.0, mag_max=3.0)
    assert w.jd_span == 2.0 and w.mag_span == 2.0
    assert AnalysisError is not None and issubclass(LightcurveError, AnalysisError)
    assert issubclass(AnalysisError, ValueError)


def test_plot_geometry_plot_rect_is_at_least_one_pixel():
    geom = PlotGeometry(480, 320)
    assert geom.plot_width == 480 - geom.margin_left - geom.margin_right
    assert geom.plot_height == 320 - geom.margin_top - geom.margin_bottom
    tiny = PlotGeometry(10, 10)  # smaller than the margins
    assert tiny.plot_width == 1 and tiny.plot_height == 1
    with pytest.raises(LightcurveError):
        PlotGeometry(-1, 320)
    with pytest.raises(LightcurveError):
        PlotGeometry(480, 320, margin_left=-5)


def test_full_window_empty_degenerate_and_padded():
    assert full_window(()) == DEFAULT_WINDOW
    # single point: degenerate jd expanded by ±1e-6, degenerate mag by ±0.05
    one = (LightcurvePoint(1, JD0, "utc", 12.5, None, "s", False),)
    w = full_window(one)
    assert w.jd_min == pytest.approx(JD0 - 1e-6)
    assert w.jd_max == pytest.approx(JD0 + 1e-6)
    assert w.mag_min == pytest.approx(12.45)
    assert w.mag_max == pytest.approx(12.55)
    # normal range: jd exact, mag padded by 5% on each side
    pts = tuple(
        LightcurvePoint(i, JD0 + 0.1 * i, "utc", 12.0 + 0.2 * i, None, "s", False)
        for i in range(5)
    )
    w = full_window(pts)
    assert w.jd_min == pytest.approx(JD0)
    assert w.jd_max == pytest.approx(JD0 + 0.4)
    assert w.mag_min == pytest.approx(12.0 - 0.05 * 0.8)
    assert w.mag_max == pytest.approx(12.8 + 0.05 * 0.8)


def test_data_screen_round_trip_and_corners():
    geom = GEOM
    # the plot rect corners map to the window corners (mag_min at the TOP)
    x0, y0 = data_to_screen(WINDOW, geom, WINDOW.jd_min, WINDOW.mag_min)
    x1, y1 = data_to_screen(WINDOW, geom, WINDOW.jd_max, WINDOW.mag_max)
    assert (x0, y0) == (geom.margin_left, geom.margin_top)  # mag_min at the TOP
    assert (x1, y1) == (
        geom.margin_left + geom.plot_width,
        geom.margin_top + geom.plot_height,
    )
    # round trip (also with a realistic Julian Date)
    for jd, mag in ((0.123, 12.34), (0.0, 13.0), (0.5, 12.0), (JD0 + 0.123, 12.34)):
        x, y = data_to_screen(WINDOW, geom, jd, mag)
        jd2, mag2 = screen_to_data(WINDOW, geom, x, y)
        assert jd2 == pytest.approx(jd)
        assert mag2 == pytest.approx(mag)


def test_mag_axis_is_inverted_brighter_at_the_top():
    geom = GEOM
    _x, y_bright = data_to_screen(WINDOW, geom, 0.25, WINDOW.mag_min)  # smaller mag
    _x, y_faint = data_to_screen(WINDOW, geom, 0.25, WINDOW.mag_max)  # larger mag
    assert y_bright < y_faint  # brighter (smaller mag) is higher on screen
    assert y_bright == geom.margin_top
    assert y_faint == geom.margin_top + geom.plot_height


def test_zoom_at_cursor_keeps_the_cursor_data_point_fixed():
    geom = GEOM
    cursor = (geom.margin_left + 0.3 * geom.plot_width, geom.margin_top + 0.7 * geom.plot_height)
    jd_c, mag_c = screen_to_data(WINDOW, geom, *cursor)
    for factor in (ZOOM_FACTOR, 2.0, 1.0 / ZOOM_FACTOR):  # in, more in, out
        zoomed = zoom_window_at(WINDOW, geom, *cursor, factor)
        # the data point under the cursor stays under the cursor
        x2, y2 = data_to_screen(zoomed, geom, jd_c, mag_c)
        assert x2 == pytest.approx(cursor[0], abs=1e-9)
        assert y2 == pytest.approx(cursor[1], abs=1e-9)
        # factor > 1 shrinks the window (zoom in), factor < 1 grows it
        if factor > 1:
            assert zoomed.jd_span == pytest.approx(WINDOW.jd_span / factor)
            assert zoomed.mag_span == pytest.approx(WINDOW.mag_span / factor)
        else:
            assert zoomed.jd_span == pytest.approx(WINDOW.jd_span / factor)


def test_zoom_in_is_clamped_to_the_minimum_spans():
    geom = GEOM
    cursor = (geom.margin_left + geom.plot_width / 2, geom.margin_top + geom.plot_height / 2)
    window = WINDOW
    for _ in range(200):  # zoom in forever
        window = zoom_window_at(window, geom, *cursor, 10.0)
    # clamped at the minimum spans (up to float representation rounding)
    assert window.jd_span == pytest.approx(MIN_JD_SPAN, rel=1e-9)
    assert window.mag_span == pytest.approx(MIN_MAG_SPAN, rel=1e-9)
    # the cursor's data point is still under the cursor (up to the float
    # rounding of the clamped ~1e-9 jd span)
    jd_c, mag_c = screen_to_data(WINDOW, geom, *cursor)
    x2, y2 = data_to_screen(window, geom, jd_c, mag_c)
    assert x2 == pytest.approx(cursor[0], abs=1e-3)
    assert y2 == pytest.approx(cursor[1], abs=1e-6)


def test_zoom_rejects_bad_factors():
    with pytest.raises(LightcurveError):
        zoom_window_at(WINDOW, GEOM, 100, 100, 0.0)
    with pytest.raises(LightcurveError):
        zoom_window_at(WINDOW, GEOM, 100, 100, -1.0)
    with pytest.raises(LightcurveError):
        zoom_window_at(WINDOW, GEOM, 100, 100, float("nan"))
    with pytest.raises(LightcurveError):
        zoom_window_at(WINDOW, GEOM, 100, 100, True)


def test_pan_shifts_the_window_so_the_content_follows_the_cursor():
    geom = GEOM
    dx, dy = 25.0, -40.0
    panned = pan_window(WINDOW, geom, dx, dy)
    # dx > 0 (dragged right) -> the window shifts toward smaller jd
    assert panned.jd_min == pytest.approx(WINDOW.jd_min - dx / geom.plot_width * WINDOW.jd_span)
    assert panned.jd_max == pytest.approx(WINDOW.jd_max - dx / geom.plot_width * WINDOW.jd_span)
    # dy < 0 (dragged up) -> the window shifts toward larger mag (fainter);
    # dy > 0 (dragged down) -> toward smaller mag (brighter, axis inverted)
    assert panned.mag_min == pytest.approx(WINDOW.mag_min - dy / geom.plot_height * WINDOW.mag_span)
    assert panned.mag_max == pytest.approx(WINDOW.mag_max - dy / geom.plot_height * WINDOW.mag_span)
    # the content follows the cursor: the data at (x, y) is now at (x+dx, y+dy)
    x, y = 200.0, 150.0
    jd, mag = screen_to_data(WINDOW, geom, x, y)
    x2, y2 = data_to_screen(panned, geom, jd, mag)
    assert x2 == pytest.approx(x + dx)
    assert y2 == pytest.approx(y + dy)
    # zero drag is a no-op
    assert pan_window(WINDOW, geom, 0.0, 0.0) == WINDOW
    with pytest.raises(LightcurveError):
        pan_window(WINDOW, geom, float("nan"), 0.0)
    with pytest.raises(LightcurveError):
        pan_window(WINDOW, geom, 0.0, "x")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_reset_view_is_full_window_of_the_points():
    pts = tuple(
        LightcurvePoint(i, JD0 + 0.1 * i, "utc", 12.0 + 0.1 * i, None, "s", False)
        for i in range(4)
    )
    window = full_window(pts)
    # after zooming + panning, the reset restores exactly the full window
    zoomed = zoom_window_at(window, GEOM, 200, 150, 3.0)
    panned = pan_window(zoomed, GEOM, 10, 10)
    assert panned != window
    assert full_window(pts) == window


# --- package boundary (subprocess + source scan) --------------------------------------------------------------


def test_pure_analysis_modules_import_no_gi_no_link_no_ui_no_photometry_and_widget_stays_lazy():
    code = (
        "import sys;"
        "import seestar_controller.analysis;"
        "import seestar_controller.analysis.lightcurve;"
        "import seestar_controller.analysis.periodogram;"
        "import seestar_controller.analysis.fold;"
        "import seestar_controller.analysis.export;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported eagerly';"
        "assert 'seestar_controller.photometry' not in sys.modules, 'photometry imported eagerly';"
        "assert 'seestar_controller.enhance' not in sys.modules, 'enhance imported eagerly';"
        "assert 'seestar_controller.analysis.lightcurve_view' not in sys.modules, "
        "'lightcurve_view imported eagerly';"
        "assert 'seestar_controller.analysis.period_view' not in sys.modules, "
        "'period_view imported eagerly';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_every_analysis_module_is_pure_or_widget_and_lazy_exported():
    modules = {path.stem for path in ANALYSIS_PACKAGE_DIR.glob("*.py")}
    assert modules == {"__init__", *PURE_ANALYSIS_MODULES, *WIDGET_ANALYSIS_MODULES}
    forbidden = re.compile(
        r"^\s*(import|from)\s+(gi\b|seestar_controller\.(gi|ui|link|photometry|enhance)\b)",
        re.MULTILINE,
    )
    for name in PURE_ANALYSIS_MODULES:
        source = (ANALYSIS_PACKAGE_DIR / f"{name}.py").read_text(encoding="utf-8")
        assert not forbidden.search(source), f"pure module {name} imports a forbidden module"
        assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), (
            f"pure module {name} imports gi"
        )
    for name in WIDGET_ANALYSIS_MODULES:
        lines = (ANALYSIS_PACKAGE_DIR / f"{name}.py").read_text(encoding="utf-8").splitlines()
        gi_lines = [i for i, line in enumerate(lines) if line.strip() == "import gi"]
        assert gi_lines, f"widget module {name} has no 'import gi' at module top"
        repo_line = next(
            i for i, line in enumerate(lines) if line.startswith("from gi.repository")
        )
        assert gi_lines[0] < repo_line, (
            f"widget module {name}: 'import gi' must come before 'from gi.repository'"
        )
    assert analysis._WIDGET_EXPORTS == {
        "LightcurveView": "lightcurve_view",
        "PeriodView": "period_view",
    }
    assert set(analysis._WIDGET_EXPORTS.values()) == set(WIDGET_ANALYSIS_MODULES)


def test_public_api_is_exported_from_package():
    for name in (
        "AXIS_COLOR",
        "DB_CALL_TIMEOUT_S",
        "DEFAULT_WINDOW",
        "EXCLUDED_COLOR",
        "MIN_JD_SPAN",
        "MIN_MAG_SPAN",
        "PLOT_MARGIN_BOTTOM",
        "PLOT_MARGIN_LEFT",
        "PLOT_MARGIN_RIGHT",
        "PLOT_MARGIN_TOP",
        "SESSION_PALETTE",
        "ZOOM_FACTOR",
        "AnalysisError",
        "InvokeOnMain",
        "LightcurveController",
        "LightcurveError",
        "LightcurveListener",
        "LightcurvePoint",
        "LightcurveSnapshot",
        "PlotGeometry",
        "PlotWindow",
        "data_to_screen",
        "full_window",
        "pan_window",
        "screen_to_data",
        "session_color",
        "session_color_index",
        "zoom_window_at",
    ):
        assert name in analysis.__all__, name
        assert hasattr(analysis, name), name
    assert "LightcurveView" in analysis.__all__  # lazy widget export
    assert lightcurve_module.LightcurveController is LightcurveController
