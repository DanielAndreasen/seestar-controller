"""Widget-layer tests for period-analysis ticket 01 — the Lightcurve view —
these SKIP in the headless sandbox (no ``gi`` module, no GTK system libs, no
display; see docs/wiki/assumptions.md) and run on the desktop, where the
system packages ``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` are installed (or
under ``xvfb-run -a uv run pytest``).

Guards, in order: ``pytest.importorskip("gi")`` (no PyGObject at all),
``gi.require_version`` for the Gtk 4.0 / Adw 1 typelibs, and a
``Gtk.init_check()`` fixture (no display -> skip) — the same pattern as
tests/test_photometry_widgets.py.

What is asserted when they run (period-analysis ticket 01): the wiring —
``build_view("lightcurve", lightcurve_controller=...)`` hands the shared
controller to the Lightcurve page (standalone construction creates its own),
the app owns one shared
:class:`~seestar_controller.analysis.lightcurve.LightcurveController`
following its shared viewer controller, and the window passes it down to the
Lightcurve page; and the
:class:`~seestar_controller.analysis.lightcurve_view.LightcurveView` behavior
against a tmp project with measurements across TWO sessions — the plot shows
one point per measurement with two distinct session colors (the legend
mapping), the plot/placeholder stack switches correctly (empty project and
no project show the placeholder label), and ``reset_zoom()`` restores the
full data window after a zoom.
"""

import io
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from astropy.io import fits as pyfits

gi = pytest.importorskip("gi")

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError as exc:  # typelibs missing
    pytest.skip(f"GTK 4.0 / libadwaita typelibs missing: {exc}", allow_module_level=True)

from gi.repository import GLib, Gtk  # noqa: E402

from seestar_controller.analysis import (  # noqa: E402  (LightcurveView is the lazy PEP 562 export)
    SESSION_PALETTE,
    LightcurveController,
    LightcurveView,
    full_window,
    zoom_window_at,
)
from seestar_controller.store import (  # noqa: E402
    KIND_SUB,
    add_measurement,
    close_session,
    create_project,
    list_sessions,
    start_session,
    store_frame,
)

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=timezone.utc)
JD0 = 2460000.5


def frozen(dt: datetime):
    return lambda: dt


def fits_bytes(date_obs: str) -> bytes:
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


def pump_main(timeout: float = 5.0) -> None:
    """Drain the default GLib main context (runs queued idle callbacks)."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if context.pending():
            context.iteration(False)
        else:
            break


@pytest.fixture()
def gtk():
    """GTK initialized, or skip when no display is available (headless)."""
    try:
        ok = Gtk.init_check()
    except Exception as exc:  # pragma: no cover - backend errors
        pytest.skip(f"Gtk.init_check failed: {exc}")
    if not ok:
        pytest.skip("no display available (headless environment)")
    return Gtk


@pytest.fixture()
def project(tmp_path):
    p = create_project("M 42 night", root=tmp_path / "projects")
    yield p
    p.close()


def add_session_points(project, *, clock, jds, mags, excluded=None) -> str:
    for open_session in list_sessions(project):
        if open_session.ended_at is None:
            close_session(project, open_session.id)
    session_id = start_session(project, clock=frozen(clock))
    excluded = excluded if excluded is not None else [False] * len(jds)
    for i, (jd, mag) in enumerate(zip(jds, mags)):
        frame = store_frame(
            project, session_id, KIND_SUB, fits_bytes(date_obs_str(10.0 * i))
        )
        add_measurement(project, frame.id, mag, jd=jd, mag_err=0.02, excluded=excluded[i])
    close_session(project, session_id)
    return session_id


@pytest.fixture()
def two_session_project(project):
    """A tmp project with measurements across TWO sessions (one excluded)."""
    session_a = add_session_points(
        project,
        clock=T0,
        jds=[JD0 + 0.10, JD0 + 0.20, JD0 + 0.30],
        mags=[12.10, 12.40, 12.20],
        excluded=[False, True, False],
    )
    session_b = add_session_points(
        project,
        clock=T0 + timedelta(days=1),
        jds=[JD0 + 0.15, JD0 + 0.25],
        mags=[12.30, 12.50],
    )
    return session_a, session_b


# --- wiring --------------------------------------------------------------------------------------------


def test_build_view_lightcurve_wires_controller(gtk):
    from seestar_controller.ui.views import LightcurveView as LightcurvePage
    from seestar_controller.ui.views import build_view

    lc = LightcurveController()
    view = build_view("lightcurve", lightcurve_controller=lc)
    assert isinstance(view, LightcurvePage)
    assert view.view_id == "lightcurve"
    assert view.lightcurve_controller is lc
    assert view.lightcurve_widget.controller is lc
    assert view.lightcurve_widget.lightcurve_controller is lc
    # standalone: no controller passed -> the view creates its own
    standalone = build_view("lightcurve")
    assert standalone.lightcurve_controller is not lc
    assert isinstance(standalone.lightcurve_widget, LightcurveView)


def test_app_owns_shared_lightcurve_controller_and_window_passes_it_down(gtk, tmp_path):
    from seestar_controller.ui.app import SeestarApplication, SeestarWindow

    app = SeestarApplication(settings_path=str(tmp_path / "settings.json"))
    assert isinstance(app.lightcurve_controller, LightcurveController)
    assert app.lightcurve_controller.viewer_controller is app.viewer_controller
    window = SeestarWindow(app, app.state)
    assert window.lightcurve_controller is app.lightcurve_controller
    page = window.view_stack.get_child_by_name("lightcurve")
    assert page.lightcurve_controller is app.lightcurve_controller
    assert page.lightcurve_widget.controller is app.lightcurve_controller


# --- the plot widget -------------------------------------------------------------------------------------


def test_lightcurve_view_two_sessions_one_color_each(gtk, project, two_session_project):
    session_a, session_b = two_session_project
    lc = LightcurveController(project)
    view = LightcurveView(lc)
    pump_main()
    assert len(view.plot_points()) == 5
    assert [p.jd for p in view.plot_points()] == sorted(p.jd for p in view.plot_points())
    colors = view.session_colors()
    assert set(colors) == {session_a, session_b}
    assert colors[session_a] == SESSION_PALETTE[0]
    assert colors[session_b] == SESSION_PALETTE[1]
    assert colors[session_a] != colors[session_b]
    # the plot page is shown (not the placeholder) and the status reports counts
    assert view.stack.get_visible_child_name() == "plot"
    assert "5 point(s)" in view.status_label.get_label()
    assert "2 session(s)" in view.status_label.get_label()
    # the excluded point is among the plotted points
    assert sum(1 for p in view.plot_points() if p.excluded) == 1
    # the initial window is the full data range
    assert view.window == full_window(view.plot_points())


def test_lightcurve_view_empty_project_shows_placeholder(gtk, project):
    lc = LightcurveController(project)
    view = LightcurveView(lc)
    pump_main()
    assert view.plot_points() == ()
    assert view.stack.get_visible_child_name() == "empty"
    assert "No measurements yet" in view.placeholder_label.get_label()


def test_lightcurve_view_no_project_shows_placeholder(gtk):
    view = LightcurveView(LightcurveController())
    pump_main()
    assert view.stack.get_visible_child_name() == "empty"
    assert "No project open" in view.placeholder_label.get_label()


def test_lightcurve_view_refresh_button_reloads(gtk, project, two_session_project):
    lc = LightcurveController(project)
    view = LightcurveView(lc)
    pump_main()
    assert len(view.plot_points()) == 5
    add_session_points(
        project, clock=T0 + timedelta(days=2), jds=[JD0 + 0.40], mags=[12.60]
    )
    view.refresh_button.emit("clicked")
    pump_main()
    assert len(view.plot_points()) == 6
    assert "6 point(s)" in view.status_label.get_label()


def test_lightcurve_view_reset_zoom_restores_full_window(gtk, project, two_session_project):
    lc = LightcurveController(project)
    view = LightcurveView(lc)
    pump_main()
    full = full_window(view.plot_points())
    assert view.window == full
    # simulate a scroll zoom-in at the plot center, then reset
    geom = view._geom
    cx = geom.margin_left + geom.plot_width / 2.0
    cy = geom.margin_top + geom.plot_height / 2.0
    view._window = zoom_window_at(view.window, geom, cx, cy, 2.0)
    assert view.window != full
    assert view.reset_zoom() == full
    assert view.window == full
    # the Reset zoom button does the same
    view._window = zoom_window_at(view.window, geom, cx, cy, 2.0)
    view.reset_button.emit("clicked")
    assert view.window == full
