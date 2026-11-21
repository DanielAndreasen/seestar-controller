"""Offline-analysis issue 02 — PURE-layer integration tests: every analysis
controller/view works on a stored project opened OFFLINE, with the link layer
mocked to FAIL (a device factory that raises), proving no analysis action
touches the scope connection.

Headless + no network: a synthetic project (8 sub-frames with a sinusoidal
variable, one WCS stack with injected stars, a cross-match table, star picks,
quality scores) is built with the store helpers, opened through
:meth:`ModeController.open_project_offline`, and then each analysis action is
exercised: viewer/editor (enhance), astrometry solve, Gaia cross-match + H-R
diagram, photometry run + lightcurve + periodogram, variable scan, quality
trends, mover analysis, mosaic stitch, and the session report. The solver
boundary (``astrometry.session.solve_frame``) and the single Gaia HTTP
boundary (``gaia.query.fetch_gaia_tap_json``) are monkeypatched; the H-R
isochrone fetcher is a raising callable (bundled fallback grid). No gi.
"""

from __future__ import annotations

import math
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_gaia_detect import make_stack, stack_fits_bytes, star_sky, wcs_header
from test_gaia_query import gaia_payload
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
    frozen,
    store_default_picks,
    var_peak,
)

from seestar_controller.analysis.fold import PeriodController
from seestar_controller.analysis.lightcurve import LightcurveController
from seestar_controller.astrometry import DriftController, SolveController, SolveResult
from seestar_controller.astrometry import session as astrometry_session
from seestar_controller.catalog.cache import QueryCache
from seestar_controller.enhance.editor_controller import EditorController
from seestar_controller.enhance.viewer_controller import ViewerController
from seestar_controller.frame_quality.session import score_session
from seestar_controller.frame_quality.trends import QualityController
from seestar_controller.gaia import query as gq
from seestar_controller.gaia.overlay import CrossmatchController
from seestar_controller.hr_diagram.controller import HrDiagramController
from seestar_controller.mosaic import mosaic_path, stitch_session
from seestar_controller.moving_objects.overlay import MoverController
from seestar_controller.observing.observe_controller import ObserveController
from seestar_controller.observing.queue_controller import PlanQueueController
from seestar_controller.photometry.picking import PickController
from seestar_controller.photometry.run import RunController
from seestar_controller.photometry.suggest import SuggestionController
from seestar_controller.report import generate_report, report_path
from seestar_controller.store import (
    KIND_ENHANCED,
    KIND_STACK,
    KIND_SUB,
    close_session,
    create_project,
    lightcurve,
    list_frames,
    load_crossmatch,
    load_frame_wcs,
    save_crossmatch,
    start_session,
    store_frame,
)
from seestar_controller.ui.connection import ConnectionController, ConnectionStatus
from seestar_controller.ui.mode import AppMode, ModeController
from seestar_controller.ui.project_controller import ProjectController
from seestar_controller.variable_scan.controller import ScanController

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

N_SUBS = 8


def _raising_fetcher(**kwargs):
    """An isochrone fetcher that always fails -> the bundled fallback grid."""
    raise OSError("no network (mocked)")


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


def _gaia_rows(header) -> list[dict]:
    """Gaia rows at the injected stars' sky positions (no jitter)."""
    rows = []
    for i, (x, y) in enumerate(ALL_XYS):
        ra, dec = star_sky(header, x, y)
        rows.append(
            {
                "source_id": 1000 + i,
                "ra": ra,
                "dec": dec,
                "g": 12.0 + i,
                "bp": 12.5 + i,
                "rp": 11.5 + i,
                "plx": 1.0,
                "pmra": 0.0,
                "pmde": 0.0,
            }
        )
    return rows


@pytest.fixture()
def offline_app(ui_harness, tmp_path):
    """The app-level pure controllers wired exactly like SeestarApplication,
    but with the link layer mocked to FAIL: the device factory raises, and
    every call is recorded in ``link_calls`` — the offline analysis must
    never touch it."""
    harness = ui_harness
    link_calls: list[str] = []

    def failing_factory(ip, key_path):
        link_calls.append(ip)
        raise OSError("link layer down (mocked)")

    connection = ConnectionController(harness.settings, device_factory=failing_factory)
    queue = PlanQueueController()
    observe = ObserveController(connection, queue, harness.bus)
    viewer = ViewerController()
    editor = EditorController(viewer, debounce_s=0.0)
    picker = PickController(viewer_controller=viewer)
    suggest = SuggestionController(picker_controller=picker, radius_deg=0.5)
    run = RunController(picker_controller=picker)
    lightcurve_ctrl = LightcurveController(viewer_controller=viewer)
    period = PeriodController(lightcurve_ctrl)
    quality = QualityController(viewer_controller=viewer)
    mover = MoverController(viewer_controller=viewer)
    crossmatch = CrossmatchController(
        viewer_controller=viewer,
        cache=QueryCache(cache_dir=tmp_path / "gaia-cache", ttl=3600.0),
    )
    hr = HrDiagramController(
        viewer_controller=viewer,
        isochrone_fetcher=_raising_fetcher,
        cache_dir=tmp_path / "iso-cache",
    )
    scan = ScanController(viewer_controller=viewer)
    solve = SolveController(viewer_controller=viewer)
    drift = DriftController(viewer_controller=viewer)
    root = tmp_path / "projects"
    root.mkdir()
    harness.state.set_setting("project_root", str(root))
    projects = ProjectController(harness.state)
    projects.attach_controllers(
        observe=observe,
        viewer=viewer,
        picker=picker,
        run=run,
        lightcurve=lightcurve_ctrl,
        period=period,
        quality=quality,
        movers=mover,
        crossmatch=crossmatch,
        hr_diagram=hr,
        scan=scan,
    )
    mode = ModeController()
    mode.attach_connection(connection)
    mode.attach_project_controller(projects)
    return SimpleNamespace(
        tmp_path=tmp_path,
        state=harness.state,
        connection=connection,
        link_calls=link_calls,
        observe=observe,
        viewer=viewer,
        editor=editor,
        picker=picker,
        suggest=suggest,
        run=run,
        lightcurve=lightcurve_ctrl,
        period=period,
        quality=quality,
        mover=mover,
        crossmatch=crossmatch,
        hr=hr,
        scan=scan,
        solve=solve,
        drift=drift,
        projects=projects,
        mode=mode,
    )


@pytest.fixture()
def stored_project(offline_app):
    """A synthetic project on disk: one closed session with 8 sub-frames
    (a sinusoidal variable at VARIABLE_XY), one WCS stack with injected
    stars at ALL_XYS, a cross-match table, the default target+comp picks,
    and persisted quality scores."""
    project = create_project("M 42 night", root=offline_app.projects.root)
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
    return SimpleNamespace(project=project, sid=sid, stack=stack, header=header)


@pytest.fixture()
def opened(offline_app, stored_project):
    """The stored project opened OFFLINE through the mode controller (the
    pure offline-open path of offline-analysis 01)."""
    project = offline_app.mode.open_project_offline(
        offline_app.projects, stored_project.project.path
    )
    return SimpleNamespace(
        app=offline_app, stored=stored_project, project=project, sid=stored_project.sid
    )


def _assert_link_untouched(app) -> None:
    """The link layer was never touched and the connection is disconnected."""
    assert app.link_calls == []
    assert app.connection.status == ConnectionStatus.DISCONNECTED
    assert app.connection.device is None


# --- offline open -----------------------------------------------------------------


def test_open_project_offline_switches_mode_and_pushes_project_to_all_controllers(opened):
    app = opened.app
    assert app.mode.mode == AppMode.OFFLINE
    assert app.mode.is_offline
    current = app.projects.current
    assert current is not None and current.path == opened.stored.project.path
    for controller in (
        app.observe,
        app.viewer,
        app.picker,
        app.run,
        app.lightcurve,
        app.period,
        app.quality,
        app.mover,
        app.crossmatch,
        app.hr,
        app.scan,
        app.solve,
        app.drift,
    ):
        assert controller.project is current
    _assert_link_untouched(app)


# --- viewer + enhance ---------------------------------------------------------------


def test_viewer_lists_and_opens_stored_stacks_offline(opened):
    app = opened.app
    stacks = app.viewer.refresh_stacks()
    assert [s.frame_id for s in stacks] == [opened.stored.stack.id]
    image = app.viewer.open_stack(opened.stored.stack.id)
    assert image.data.shape == (SIZE, SIZE)
    assert app.viewer.current_stack is not None
    assert app.viewer.current_stack.frame_id == opened.stored.stack.id
    _assert_link_untouched(app)


def test_enhance_save_writes_enhanced_frame_back_to_project_offline(opened):
    app = opened.app
    app.viewer.refresh_stacks()
    app.viewer.open_stack(opened.stored.stack.id)
    frame = app.editor.save_enhanced()
    assert frame.kind == KIND_ENHANCED
    stored = list_frames(app.projects.current, kind=KIND_ENHANCED)
    assert [f.id for f in stored] == [frame.id]
    _assert_link_untouched(app)


# --- astrometry solve ----------------------------------------------------------------


def test_solve_writes_wcs_back_to_project_offline(opened, monkeypatch):
    app = opened.app
    app.viewer.refresh_stacks()
    app.viewer.open_stack(opened.stored.stack.id)
    monkeypatch.setattr(
        astrometry_session, "solve_frame", lambda **kwargs: _fake_solve_result()
    )
    snap = app.solve.solve_open_stack()
    assert snap.error is None
    assert snap.solved is not None and snap.solved.solver == "gaia"
    # persisted in the store (written back to the project)
    assert load_frame_wcs(app.projects.current, opened.stored.stack.id) is not None
    _assert_link_untouched(app)


# --- gaia crossmatch + H-R diagram -----------------------------------------------------


def test_crossmatch_runs_offline_and_persists_table(opened, monkeypatch):
    app = opened.app
    rows = _gaia_rows(opened.stored.header)
    calls: list[str] = []

    def fake_fetch(query, **kwargs):
        calls.append(query)
        match = re.search(r'"Gmag" < ([0-9.eE+-]+)', query)
        limit = float(match.group(1)) if match else 99.0
        return gaia_payload([row for row in rows if row["g"] < limit])

    monkeypatch.setattr(gq, "fetch_gaia_tap_json", fake_fetch)
    app.viewer.refresh_stacks()
    app.viewer.open_stack(opened.stored.stack.id)
    app.crossmatch.select_stack(opened.stored.stack.id)
    snap = app.crossmatch.run_crossmatch()
    assert snap.error is None
    assert calls, "the Gaia TAP boundary was queried (mocked)"
    assert len(snap.rows) >= 1
    # written back to the project
    persisted = load_crossmatch(app.projects.current, opened.stored.stack.id)
    assert len(persisted) == len(snap.rows)
    _assert_link_untouched(app)


def test_hr_diagram_reads_crossmatched_stack_offline(opened, tmp_path):
    app = opened.app
    stacks = app.hr.refresh_stacks()
    assert [s.frame_id for s in stacks] == [opened.stored.stack.id]
    app.hr.select_stack(opened.stored.stack.id)
    snap = app.hr.refresh()
    assert snap.error is None
    assert len(snap.table) == len(ALL_XYS)
    assert len(snap.cmd) == len(ALL_XYS)
    out = app.hr.export_table(tmp_path / "hr.csv")
    assert out.exists() and out.stat().st_size > 0
    _assert_link_untouched(app)


# --- photometry run + lightcurve + period ----------------------------------------------


def test_photometry_run_measures_stored_subs_and_writes_lightcurve_offline(opened):
    app = opened.app
    report = app.run.run_session(opened.sid)
    assert len(report.measurements) == N_SUBS
    assert report.session_id == opened.sid
    # written back to the project: the store lightcurve has all 8 points
    assert len(lightcurve(app.projects.current)) == N_SUBS
    snap = app.lightcurve.refresh()
    assert snap.error is None
    assert len(snap.points) == N_SUBS
    assert opened.sid in snap.sessions
    _assert_link_untouched(app)


def test_periodogram_and_fold_run_offline(opened):
    app = opened.app
    app.run.run_session(opened.sid)
    app.lightcurve.refresh()
    result = app.period.compute_periodogram()
    assert result.peaks, "the periodogram has peaks"
    period = app.period.select_peak(0)
    assert period > 0.0
    assert len(app.period.folded_points()) == N_SUBS
    _assert_link_untouched(app)


# --- variable scan ----------------------------------------------------------------------


def test_variable_scan_finds_the_injected_variable_offline(opened, tmp_path):
    app = opened.app
    snap = app.scan.run_scan()
    assert snap.error is None
    assert len(snap.candidates) >= 1
    top = snap.candidates[0]
    assert (top.x, top.y) == pytest.approx((ALL_XYS[4][0], ALL_XYS[4][1]), abs=1.5)
    out = app.scan.export_csv(tmp_path / "scan.csv")
    assert out.exists() and out.stat().st_size > 0
    _assert_link_untouched(app)


# --- quality trends + movers -------------------------------------------------------------


def test_quality_trends_read_stored_scores_offline(opened):
    app = opened.app
    snap = app.quality.select_session(opened.sid)
    assert snap.error is None
    snap = app.quality.refresh()
    assert snap.error is None
    assert opened.sid in snap.sessions
    assert snap.trends is not None
    _assert_link_untouched(app)


def test_mover_analysis_runs_offline(opened):
    app = opened.app
    app.mover.select_session(opened.sid)
    snap = app.mover.analyze_session()
    assert snap.error is None
    assert len(snap.frames) == N_SUBS
    _assert_link_untouched(app)


# --- mosaic stitch + session report -------------------------------------------------------


def test_mosaic_stitch_writes_mosaic_back_to_project_offline(opened):
    app = opened.app
    path = stitch_session(app.projects.current, opened.sid)
    assert path == mosaic_path(app.projects.current, opened.sid)
    assert path.exists() and path.stat().st_size > 0
    _assert_link_untouched(app)


def test_session_report_writes_html_back_to_project_offline(opened):
    app = opened.app
    path = generate_report(app.projects.current, opened.sid, clock=frozen(T0 + timedelta(hours=2)))
    assert path == report_path(app.projects.current, opened.sid)
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "<html" in text.lower()
    assert opened.sid in text
    _assert_link_untouched(app)
