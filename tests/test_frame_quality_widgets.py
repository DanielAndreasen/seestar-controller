"""Widget-layer tests for frame-quality ticket 02 — the session-quality view
— these SKIP in the headless sandbox (no ``gi`` module, no GTK system libs,
no display; see docs/wiki/assumptions.md) and run on the desktop, where the
system packages ``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` are installed (or
under ``xvfb-run -a uv run pytest``).

Guards, in order: ``pytest.importorskip("gi")`` (no PyGObject at all),
``gi.require_version`` for the Gtk 4.0 / Adw 1 typelibs, and a
``Gtk.init_check()`` fixture (no display -> skip) — the same pattern as
tests/test_analysis_widgets.py.

What is asserted when they run: the wiring —
``build_view("lightcurve", quality_controller=...)`` hands the shared
controller to the Lightcurve page's session-quality section (standalone
construction creates its own), the app owns one shared
:class:`~seestar_controller.frame_quality.QualityController` following its
shared viewer controller, and the window passes it down to the Lightcurve
page; and the
:class:`~seestar_controller.frame_quality.quality_view.SessionQualityView`
behavior against a tmp project with saved quality records across TWO
sessions — the session dropdown lists both sessions, the summary card shows
the selected session's counts / rejected percent / median FWHM / median
background, the three trend plots (FWHM / background / airmass vs time) carry
one point per scored frame, switching the dropdown selects the other session
on the controller, Refresh re-reads the store, and the no-project state
shows its hint. The :class:`~...quality_view.TrendPlot` series/window
helpers are checked directly too (values alignment, the full-data window
reset, the placeholder).
"""

import io
import json
import time
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from astropy.io import fits as pyfits

gi = pytest.importorskip("gi")

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError as exc:  # typelibs missing
    pytest.skip(f"GTK 4.0 / libadwaita typelibs missing: {exc}", allow_module_level=True)

from gi.repository import GLib, Gtk

from seestar_controller.frame_quality import (
    MAX_AIRMASS,
    QualityController,
    SessionQualityView,
    TrendPlot,
    full_trend_window,
)
from seestar_controller.frame_quality.quality_view import (
    SessionQualityView as SessionQualityViewDirect,
)
from seestar_controller.store import (
    KIND_SUB,
    close_session,
    create_project,
    list_sessions,
    save_frame_quality,
    start_session,
    store_frame,
)

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)

#: The session target coords (stored as the JSON sidecar text via meta).
TARGET_COORDS = {"ra": 5.5, "dec": 22.0}


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
    except Exception as exc:  # pragma: no cover - backend errors  # noqa: BLE001
        pytest.skip(f"Gtk.init_check failed: {exc}")
    if not ok:
        pytest.skip("no display available (headless environment)")
    return Gtk


@pytest.fixture()
def project(tmp_path):
    p = create_project("M 42 night", root=tmp_path / "projects")
    yield p
    p.close()


def add_scored_session(
    project,
    *,
    clock,
    n_frames: int = 3,
    fwhms=(3.5, 3.6, 3.7),
    flagged_index: int | None = None,
    with_coords: bool = True,
) -> str:
    """One closed session with ``n_frames`` scored sub frames; returns its id."""
    for open_session in list_sessions(project):
        if open_session.ended_at is None:
            close_session(project, open_session.id)
    session_id = start_session(project, clock=frozen(clock))
    meta = {"target": dict(TARGET_COORDS)} if with_coords else {"target": {"name": "M 42"}}
    for i in range(n_frames):
        frame = store_frame(
            project, session_id, KIND_SUB, fits_bytes(date_obs_str(10.0 * i)), meta=meta
        )
        save_frame_quality(
            project,
            frame.id,
            fwhm=fwhms[i % len(fwhms)],
            bg_median=100.0 + i,
            bg_std=5.0,
            star_count=10,
            flagged=(i == flagged_index),
            flag_reasons=("fwhm",) if i == flagged_index else (),
        )
    close_session(project, session_id, clock=frozen(clock + timedelta(hours=1)))
    return session_id


@pytest.fixture()
def two_session_project(project):
    """A tmp project with scored frames across TWO sessions (one flagged)."""
    session_a = add_scored_session(project, clock=T0, n_frames=3, flagged_index=1)
    session_b = add_scored_session(
        project, clock=T0 + timedelta(days=1), n_frames=2, fwhms=(4.0, 4.1)
    )
    return session_a, session_b


# --- wiring ---------------------------------------------------------------------------------------


def test_build_view_lightcurve_wires_quality_controller(gtk):
    from seestar_controller.ui.views import LightcurveView as LightcurvePage
    from seestar_controller.ui.views import build_view

    qc = QualityController()
    view = build_view("lightcurve", quality_controller=qc)
    assert isinstance(view, LightcurvePage)
    assert view.quality_controller is qc
    assert view.quality_widget.controller is qc
    # standalone: no controller passed -> the view creates its own
    standalone = build_view("lightcurve")
    assert standalone.quality_controller is not qc
    assert isinstance(standalone.quality_widget, SessionQualityView)


def test_app_owns_shared_quality_controller_and_window_passes_it_down(gtk, tmp_path):
    from seestar_controller.ui.app import SeestarApplication, SeestarWindow

    app = SeestarApplication(settings_path=str(tmp_path / "settings.json"))
    assert isinstance(app.quality_controller, QualityController)
    assert app.quality_controller.viewer_controller is app.viewer_controller
    window = SeestarWindow(app, app.state)
    assert window.quality_controller is app.quality_controller
    page = window.view_stack.get_child_by_name("lightcurve")
    assert page.quality_controller is app.quality_controller
    assert page.quality_widget.controller is app.quality_controller


def test_lazy_widget_export_matches_direct_import(gtk):
    import seestar_controller.frame_quality as fq

    assert fq.SessionQualityView is SessionQualityViewDirect


# --- the TrendPlot widget ---------------------------------------------------------------------------


def test_trend_plot_series_window_and_placeholder(gtk, project, two_session_project):
    from seestar_controller.frame_quality.trends import session_trends

    session_a, _ = two_session_project
    trends = session_trends(project, session_a)
    plot = TrendPlot("FWHM vs time", "FWHM (px)")
    assert plot.title == "FWHM vs time" and plot.y_label == "FWHM (px)"
    assert plot.points() == () and plot.values() == ()
    plot.set_series(trends.points, tuple(p.fwhm for p in trends.points))
    assert plot.points() == trends.points
    assert plot.values() == (3.5, 3.6, 3.7)
    # the window reset to the full data range (x = jd, y = fwhm)
    assert plot.window == full_trend_window(trends.jds, [3.5, 3.6, 3.7])
    # empty series -> the default window
    plot.set_series((), (), placeholder="nothing here")
    assert plot.points() == () and plot.values() == ()
    from seestar_controller.frame_quality import DEFAULT_TREND_WINDOW

    assert plot.window == DEFAULT_TREND_WINDOW
    # misaligned values are rejected
    with pytest.raises(ValueError, match="align"):
        plot.set_series(trends.points, (3.5,))


# --- the SessionQualityView widget -------------------------------------------------------------------


def test_quality_view_renders_summary_and_plots(gtk, project, two_session_project):
    session_a, session_b = two_session_project
    qc = QualityController(project)
    view = SessionQualityView(qc)
    pump_main()
    # the dropdown lists both sessions, the first selected
    assert view.refresh_sessions() == [session_a, session_b]
    assert view.selected_session_id() == session_a
    # the summary card
    assert view.session_value_label.get_label() == session_a
    assert view.target_value_label.get_label() == json.dumps(TARGET_COORDS, sort_keys=True)
    assert view.frames_value_label.get_label() == "3"
    assert view.flagged_value_label.get_label() == "1 (33.3%)"
    assert view.fwhm_value_label.get_label() == "3.60 px"
    assert view.background_value_label.get_label() == "101.0 ADU"
    # the three trend plots carry one point per scored frame
    assert len(view.fwhm_plot.points()) == 3
    assert view.fwhm_plot.values() == (3.5, 3.6, 3.7)
    assert len(view.background_plot.points()) == 3
    assert view.background_plot.values() == (100.0, 101.0, 102.0)
    assert len(view.airmass_plot.points()) == 3
    assert all(1.0 <= a <= MAX_AIRMASS for a in view.airmass_plot.values())
    # the status line reports the counts
    assert "3 scored frame(s)" in view.status_label.get_label()
    assert "1 rejected" in view.status_label.get_label()


def test_quality_view_session_switch_selects_on_controller(gtk, project, two_session_project):
    session_a, session_b = two_session_project
    qc = QualityController(project)
    view = SessionQualityView(qc)
    pump_main()
    assert view.selected_session_id() == session_a
    # switch the dropdown to the second session
    view.session_dropdown.set_selected(1)
    pump_main()
    assert view.selected_session_id() == session_b
    assert qc.selected_session == session_b
    assert view.session_value_label.get_label() == session_b
    assert view.frames_value_label.get_label() == "2"
    assert view.flagged_value_label.get_label() == "0 (0.0%)"
    assert view.fwhm_value_label.get_label() == "4.05 px"
    assert len(view.fwhm_plot.points()) == 2


def test_quality_view_refresh_button_reloads(gtk, project, two_session_project):
    session_a, _ = two_session_project
    qc = QualityController(project)
    view = SessionQualityView(qc)
    pump_main()
    assert view.selected_session_id() == session_a
    # a third session appears in the store
    session_c = add_scored_session(project, clock=T0 + timedelta(days=2), n_frames=1)
    view.refresh_button.clicked()
    pump_main()
    sessions = view.refresh_sessions()
    assert session_c in sessions
    assert len(sessions) == 3
    # the selection is kept (session_a still exists)
    assert view.selected_session_id() == session_a


def test_quality_view_no_coords_shows_airmass_placeholder(gtk, project):
    sid = add_scored_session(project, clock=T0, n_frames=2, with_coords=False)
    qc = QualityController(project)
    view = SessionQualityView(qc)
    pump_main()
    assert view.selected_session_id() == sid
    assert view.target_value_label.get_label() == "M 42"
    assert view.airmass_plot.points() == ()
    assert view.airmass_plot.values() == ()
    # the fwhm/background plots still carry the points
    assert len(view.fwhm_plot.points()) == 2


def test_quality_view_no_project_shows_hint(gtk):
    view = SessionQualityView(QualityController())
    pump_main()
    assert view.selected_session_id() is None
    assert "No project open" in view.status_label.get_label()
    assert view.frames_value_label.get_label() == "0"
    assert view.fwhm_plot.points() == ()


def test_quality_view_empty_project_shows_no_sessions_hint(gtk, project):
    qc = QualityController(project)
    view = SessionQualityView(qc)
    pump_main()
    assert view.selected_session_id() is None
    assert "No sessions yet" in view.status_label.get_label()
