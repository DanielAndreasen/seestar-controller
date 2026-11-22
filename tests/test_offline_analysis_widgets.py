"""Widget-layer tests for offline-analysis issue 02 — the analysis views
operating on a stored project opened OFFLINE: the Projects view's
session/frame/stack browser populated from the store (with the Stitch mosaic
/ Session report actions writing their products back into the project), and
the analysis views (enhance/solve, photometry run, lightcurve, H-R diagram)
rendering the stored data with the link layer never touched.

These SKIP in the headless sandbox (no ``gi`` module, no GTK system libs, no
display; see docs/wiki/assumptions.md) and run on the desktop (or under
``xvfb-run -a uv run pytest``) — the same guard pattern as
tests/test_offline_mode_widgets.py / tests/test_gaia_overlay_widgets.py.
Buttons are clicked with ``.emit("clicked")`` (never ``.clicked()``) and no
widget is ever ``destroy()``-ed (GTK4). The solver boundary
(``astrometry.session.solve_frame``) is monkeypatched; no network.
"""

import math
import time

import pytest
from test_gaia_detect import make_stack, stack_fits_bytes, wcs_header
from test_variable_scan import (
    ALL_XYS,
    AMP,
    F0,
    SIZE,
    T0,
    crossmatch_rows,
    date_obs_str,
    fits_bytes,
    frame_data,
    store_default_picks,
    var_peak,
)

gi = pytest.importorskip("gi")

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError as exc:  # typelibs missing
    pytest.skip(f"GTK 4.0 / libadwaita typelibs missing: {exc}", allow_module_level=True)

from gi.repository import GLib, Gtk

from seestar_controller.astrometry import SolveResult
from seestar_controller.astrometry import session as astrometry_session
from seestar_controller.frame_quality.session import score_session
from seestar_controller.mosaic import mosaic_path
from seestar_controller.report import report_path
from seestar_controller.store import (
    KIND_STACK,
    KIND_SUB,
    close_session,
    create_project,
    lightcurve,
    load_frame_wcs,
    save_crossmatch,
    start_session,
    store_frame,
)
from seestar_controller.ui.connection import ConnectionStatus
from seestar_controller.ui.mode import AppMode

N_SUBS = 8

#: A solved-WCS card set (TAN around the synthetic field center).
CARDS = {
    "CTYPE1": "RA---TAN",
    "CTYPE2": "DEC--TAN",
    "CRVAL1": 83.633,
    "CRVAL2": 22.014,
    "CRPIX1": SIZE / 2 + 0.5,
    "CRPIX2": SIZE / 2 + 0.5,
    "CDELT1": -0.01,
    "CDELT2": 0.01,
}


def _fake_solve_result() -> SolveResult:
    return SolveResult(
        wcs=None,
        cards=dict(CARDS),
        solver="gaia",
        solved_at="2026-11-20T22:05:00",
        center_ra_deg=83.633,
        center_dec_deg=22.014,
        n_stars=len(ALL_XYS),
        rms_arcsec=0.4,
        matches=(),
        rows=(),
    )


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
def app(gtk, tmp_path):
    """A SeestarApplication with its settings file in tmp_path and a scope IP
    set (pattern: tests/test_offline_mode_widgets.py)."""
    from seestar_controller.ui.app import SeestarApplication

    application = SeestarApplication(settings_path=str(tmp_path / "settings.json"))
    application.state.set_setting("scope_ip", "192.168.1.50")
    return application


@pytest.fixture()
def project_root(app, tmp_path):
    """Point the app's project root at a tmp dir and refresh the list."""
    root = tmp_path / "projects"
    root.mkdir()
    app.state.set_setting("project_root", str(root))
    app.state.save()
    app.project_controller.refresh()
    return root


@pytest.fixture()
def stored_project(project_root):
    """A synthetic project on disk: one closed session with 8 sub-frames (a
    sinusoidal variable), one WCS stack with injected stars, a cross-match
    table, the default picks, and persisted quality scores."""
    project = create_project("M 42 night", root=project_root)
    sid = start_session(project, started_at=T0)
    for i in range(N_SUBS):
        m_inj = AMP * math.sin(2.0 * math.pi * i / N_SUBS)
        store_frame(
            project,
            sid,
            KIND_SUB,
            fits_bytes(frame_data(var_peak(m_inj), seed=100 + i), date_obs=date_obs_str(i)),
        )
    header = wcs_header(size=SIZE)
    header["DATE-OBS"] = date_obs_str(50)
    stars = [(y, x, F0) for (x, y) in ALL_XYS]
    stack = store_frame(
        project,
        sid,
        KIND_STACK,
        stack_fits_bytes(make_stack(stars=stars, size=SIZE, seed=999), header),
    )
    close_session(project, sid)
    save_crossmatch(project, stack.id, crossmatch_rows())
    store_default_picks(project)
    score_session(project, sid)
    return {"project": project, "sid": sid, "stack": stack}


def _pump() -> None:
    """Run all pending GLib main-context callbacks (the app's
    ``invoke_on_main`` is ``GLib.idle_add``)."""
    ctx = GLib.MainContext.default()
    while ctx.pending():
        ctx.iteration(False)


def _pump_until(predicate, timeout: float = 20.0) -> bool:
    """Pump the main context until ``predicate()`` is true (worker-thread
    results + marshalled store calls land on the main loop)."""
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while ctx.pending():
            ctx.iteration(False)
        if predicate():
            return True
        time.sleep(0.02)
    while ctx.pending():
        ctx.iteration(False)
    return predicate()


def _projects_page(window):
    from seestar_controller.ui.views import ProjectsView

    page = window.view_stack.get_child_by_name("projects")
    assert isinstance(page, ProjectsView)
    return page


def _open_offline(app, stored):
    """A window with the synthetic project opened offline."""
    from seestar_controller.ui.app import SeestarWindow

    app.project_controller.refresh()
    window = SeestarWindow(app, app.state)
    window.open_project_offline(stored["project"].path)
    _pump()
    return window


def _assert_link_untouched(window) -> None:
    assert window.connection_controller.status == ConnectionStatus.DISCONNECTED
    assert window.connection_controller.device is None


# --- the stored-data browser in the Projects view ---------------------------------


def test_projects_view_browser_lists_sessions_and_frames_offline(app, gtk, stored_project):
    window = _open_offline(app, stored_project)
    assert window.mode == AppMode.OFFLINE
    assert window.offline_banner.get_revealed() is True
    page = _projects_page(window)
    # the browser is populated from the store
    assert page.session_count() == 1
    assert page.session_id(0) == stored_project["sid"]
    assert "closed" in page.session_row_title(0)
    subtitle = page.session_row_subtitle(0)
    assert f"{N_SUBS} subs" in subtitle
    assert "1 stacks" in subtitle
    # one row per stored frame (8 subs + 1 stack)
    assert page.frame_row_count(0) == N_SUBS + 1
    assert "sub" in page.frame_row_label(0, 0)
    assert "stack" in page.frame_row_label(0, N_SUBS)
    assert "M 42 night" in page.data_label.get_label()
    _assert_link_untouched(window)


def test_stitch_and_report_buttons_write_back_into_the_project_offline(
    app, gtk, stored_project
):
    window = _open_offline(app, stored_project)
    page = _projects_page(window)
    project = app.project_controller.current
    sid = stored_project["sid"]

    page.click_stitch(0)
    _pump()
    mosaic = mosaic_path(project, sid)
    assert mosaic.exists() and mosaic.stat().st_size > 0
    assert "stitched" in page.data_status_label.get_label()

    page.click_report(0)
    _pump()
    report = report_path(project, sid)
    assert report.exists() and report.stat().st_size > 0
    assert "report" in page.data_status_label.get_label()
    _assert_link_untouched(window)


# --- the analysis views render the stored project offline ---------------------------


def test_analysis_views_render_stored_data_offline(app, gtk, stored_project):
    from seestar_controller.ui.views import (
        EnhanceView,
        HrDiagramView,
        LightcurveView,
        PhotometryView,
    )

    window = _open_offline(app, stored_project)
    project = app.project_controller.current
    # the shared controllers all carry the offline-opened project
    assert app.viewer_controller.project is project
    stacks = app.viewer_controller.refresh_stacks()
    # image-enhance issue 05: the DEFAULT list is the single stack only
    # (newest first); the 8 subs sit behind the show-all toggle
    assert [s.kind for s in stacks] == ["stack"]
    assert stacks[0].frame_id == stored_project["stack"].id
    assert [s.kind for s in app.viewer_controller.all_stacks] == ["stack"] + ["sub"] * N_SUBS

    cases = (
        ("enhance", EnhanceView),
        ("photometry", PhotometryView),
        ("lightcurve", LightcurveView),
        ("hr-diagram", HrDiagramView),
    )
    for view_id, cls in cases:
        window.select_view(view_id)
        _pump()
        page = window.view_stack.get_child_by_name(view_id)
        assert isinstance(page, cls), view_id
    _assert_link_untouched(window)


def test_solve_button_plate_solves_the_stored_stack_offline(
    app, gtk, stored_project, monkeypatch
):
    window = _open_offline(app, stored_project)
    project = app.project_controller.current
    stack_id = stored_project["stack"].id
    app.viewer_controller.refresh_stacks()
    app.viewer_controller.open_stack(stack_id)
    _pump()

    monkeypatch.setattr(
        astrometry_session, "solve_frame", lambda **kwargs: _fake_solve_result()
    )
    window.select_view("enhance")
    _pump()
    from seestar_controller.ui.views import EnhanceView

    page = window.view_stack.get_child_by_name("enhance")
    assert isinstance(page, EnhanceView)
    panel = page.solve_panel
    panel.solve_button.emit("clicked")
    assert _pump_until(lambda: not panel.busy), "solve worker did not finish"
    snap = app.solve_controller.snapshot
    assert snap.error is None
    assert snap.solved is not None
    # written back into the project
    assert load_frame_wcs(project, stack_id) is not None
    _assert_link_untouched(window)


def test_photometry_run_button_measures_the_stored_session_offline(app, gtk, stored_project):
    window = _open_offline(app, stored_project)
    project = app.project_controller.current
    sid = stored_project["sid"]
    window.select_view("photometry")
    _pump()
    from seestar_controller.ui.views import PhotometryView

    page = window.view_stack.get_child_by_name("photometry")
    assert isinstance(page, PhotometryView)
    panel = page.results_panel
    panel.run_button.emit("clicked")
    assert _pump_until(
        lambda: len(lightcurve(project)) == N_SUBS, timeout=30.0
    ), "photometry run did not store the measurements"
    snap = app.run_controller.snapshot
    assert snap.session_id == sid
    assert snap.report is not None and len(snap.report.measurements) == N_SUBS
    _assert_link_untouched(window)
