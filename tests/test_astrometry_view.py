"""Astrometry ticket 03 — the PURE layer tests: the annotated-stack render
(:mod:`seestar_controller.astrometry.annotate`), the stored-frame export
(:mod:`.export`), the drift-plot PNG render (:mod:`.plot`), and the
app-level controllers (:mod:`.controller` — SolveController /
DriftController, with a duck-typed fake viewer).

Headless + no network: synthetic stacks/projects only (helpers from
test_gaia_detect / test_gaia_overlay / test_astrometry_drift); the single
solver boundary ``astrometry.session.solve_frame`` is monkeypatched. No gi.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest
from astropy.io import fits as pyfits
from test_gaia_detect import STARS, make_stack, wcs_header

from seestar_controller.astrometry import (
    AstrometryError,
    DriftController,
    DriftError,
    DriftSeries,
    ExportError,
    SolveController,
    SolveError,
    SolveResult,
    annotated_png_bytes,
    annotated_stack_png,
    default_export_name,
    drift_series,
    export_annotated_png,
    format_scale_bar,
    fov_corner_sky,
    render_annotated_stack,
    render_drift_png,
    scale_bar_arcsec,
)
from seestar_controller.astrometry import session as astrometry_session
from seestar_controller.gaia.matching import CrossmatchRow
from seestar_controller.store import (
    KIND_STACK,
    KIND_SUB,
    close_session,
    create_project,
    load_frame_wcs,
    open_project,
    read_frame_bytes,
    save_crossmatch,
    save_frame_wcs,
    start_session,
    store_frame,
)
from seestar_controller.store.frames import header_to_dict

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)

#: A solved-WCS card set (TAN around the requested target).
CARDS = {
    "CTYPE1": "RA---TAN",
    "CTYPE2": "DEC--TAN",
    "CRVAL1": 83.0,
    "CRVAL2": 22.0,
    "CRPIX1": 128.5,
    "CRPIX2": 128.5,
    "CDELT1": -0.001,
    "CDELT2": 0.001,
}
REQ_RA_DEG = 83.0
REQ_DEC_DEG = 22.0

PNG_MAGIC = b"\x89PNG"


# --- fixtures ---------------------------------------------------------------


@pytest.fixture()
def project(tmp_path):
    p = create_project("M 42", root=tmp_path / "projects")
    yield p
    p.close()


def _fits_bytes(data, header, date_obs: str = "2026-11-20T22:00:10.000") -> bytes:
    hdu = pyfits.PrimaryHDU(data=np.asarray(data, dtype=np.float32), header=header)
    hdu.header["DATE-OBS"] = date_obs
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


@pytest.fixture()
def stack_frame(project):
    """One session STACK with a WCS + the injected stars; returns
    (frame, header)."""
    sid = start_session(project, started_at=T0)
    header = wcs_header()
    frame = store_frame(project, sid, KIND_STACK, _fits_bytes(make_stack(), header))
    return frame, header


@pytest.fixture()
def plain_stack_frame(project):
    """One session STACK WITHOUT a celestial WCS (no CTYPE cards) — the
    solve target for the solve tests, so the WCS write is observable."""
    sid = start_session(project, started_at=T0)
    header = pyfits.Header()
    frame = store_frame(project, sid, KIND_STACK, _fits_bytes(make_stack(), header))
    return frame


def _rows() -> tuple:
    return (
        SimpleNamespace(x=20.0, y=30.0, matched=True, g=12.34),
        SimpleNamespace(x=60.0, y=70.0, matched=False, g=None),
    )


# --- annotate ---------------------------------------------------------------


def test_render_annotated_stack_rgb_size_and_rows_change_pixels():
    data = make_stack(stars=STARS)
    header = header_to_dict(wcs_header())
    plain = render_annotated_stack(data, (), header=header, max_size=64)
    assert plain.mode == "RGB"
    assert max(plain.size) <= 64
    annotated = render_annotated_stack(data, _rows(), header=header, max_size=64)
    assert annotated.size == plain.size
    assert not np.array_equal(np.asarray(annotated), np.asarray(plain))
    # the default max_size keeps a small stack at its native size
    full = render_annotated_stack(data, _rows(), header=header)
    assert full.size == (data.shape[1], data.shape[0])


def test_annotated_png_bytes_has_png_magic():
    png = annotated_png_bytes(make_stack(), _rows(), header=header_to_dict(wcs_header()))
    assert png.startswith(PNG_MAGIC)


def test_render_annotated_stack_without_wcs_still_renders():
    header = {"DATE-OBS": "2026-11-20T22:00:10.000"}  # no CTYPE -> no celestial WCS
    image = render_annotated_stack(make_stack(), _rows(), header=header, max_size=64)
    assert image.mode == "RGB"
    assert fov_corner_sky(header, (64, 64)) is None


def test_render_annotated_stack_rejects_bad_inputs():
    data = make_stack()
    with pytest.raises(ExportError):
        render_annotated_stack(data, max_size=0)
    with pytest.raises(ExportError):
        render_annotated_stack(data, max_size=True)
    with pytest.raises(ExportError):
        render_annotated_stack(data, lo_percentile=101.0)
    with pytest.raises(ExportError):
        render_annotated_stack(np.zeros(5, dtype=np.float32))  # not 2-D
    with pytest.raises(ExportError):
        render_annotated_stack(data, [SimpleNamespace(x="a", y=1.0, matched=False)])
    with pytest.raises(ExportError):
        render_annotated_stack(
            data, [SimpleNamespace(x=1.0, y=1.0, matched=True, g=None)]
        )
    with pytest.raises(TypeError):
        render_annotated_stack(data, header=cast(Any, "not-a-mapping"))


def test_fov_corner_sky_with_wcs_returns_four_corners():
    header = header_to_dict(wcs_header())
    corners = fov_corner_sky(header, (128, 128))
    assert corners is not None and len(corners) == 4
    for ra, dec in corners:
        assert 0.0 <= ra < 360.0
        assert -90.0 <= dec <= 90.0
    # the field center (CRVAL) lies inside the corner box
    ras = [c[0] for c in corners]
    decs = [c[1] for c in corners]
    assert min(ras) <= 83.633 <= max(ras)
    assert min(decs) <= 22.014 <= max(decs)
    with pytest.raises(ExportError):
        fov_corner_sky(header, (0, 10))
    assert fov_corner_sky(None, (10, 10)) is None


def test_scale_bar_arcsec_picks_nice_length_and_format():
    # 36 arcsec/px over 128 px -> 1152 arcsec max bar -> largest nice <= that
    assert scale_bar_arcsec(36.0, 128) == 900.0
    assert scale_bar_arcsec(0.001, 10) is None  # nothing nice fits
    assert scale_bar_arcsec(0.0, 100) is None
    assert scale_bar_arcsec(36.0, 0) is None
    assert format_scale_bar(3600.0) == "1°"
    assert format_scale_bar(300.0) == "5′"
    assert format_scale_bar(30.0) == "30″"


# --- export -----------------------------------------------------------------


def test_annotated_stack_png_and_atomic_export(project, stack_frame, tmp_path):
    frame, _header = stack_frame
    save_crossmatch(
        project,
        frame.id,
        [
            CrossmatchRow(x=20.0, y=30.0, ra_deg=83.6, dec_deg=22.0, matched=True, source_id=1, g=12.34),
            CrossmatchRow(x=60.0, y=70.0, ra_deg=83.7, dec_deg=22.1, matched=False),
        ],
    )
    png = annotated_stack_png(project, frame.id)
    assert png.startswith(PNG_MAGIC)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out = export_annotated_png(project, frame.id, out_dir / "ann.png")
    assert out == out_dir / "ann.png"
    assert out.read_bytes().startswith(PNG_MAGIC)
    # no temp file left behind
    assert list(out_dir.iterdir()) == [out]
    assert default_export_name(frame) == "annotated-20261120T220010.png"


def test_export_unknown_frame_raises_and_writes_nothing(project, tmp_path):
    with pytest.raises(ExportError):
        annotated_stack_png(project, 9999)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out = out_dir / "ann.png"
    with pytest.raises(ExportError):
        export_annotated_png(project, 9999, out)
    assert not out.exists()
    assert list(out_dir.iterdir()) == []


# --- plot -------------------------------------------------------------------


def _drift_project(project):
    """One session with three solved subs (10 s apart, small RA offsets) and
    a config target; returns (session_id, frames)."""
    project.config["target"] = {"name": "M 42", "ra_deg": REQ_RA_DEG, "dec_deg": REQ_DEC_DEG}
    project.save_config()
    sid = start_session(project, started_at=T0)
    frames = []
    for i in range(3):
        instant = T0 + timedelta(seconds=10 * i)
        hdu = pyfits.PrimaryHDU(data=[[1.0, 2.0], [3.0, 4.0]])
        hdu.header["DATE-OBS"] = instant.isoformat(timespec="milliseconds")
        buf = io.BytesIO()
        pyfits.HDUList([hdu]).writeto(buf)
        frame = store_frame(project, sid, KIND_SUB, buf.getvalue())
        save_frame_wcs(
            project,
            frame.id,
            solver="gaia",
            n_stars=5,
            center_ra_deg=REQ_RA_DEG + (i + 1) * 1.0 / 3600.0,
            center_dec_deg=REQ_DEC_DEG,
            cards=CARDS,
        )
        frames.append(frame)
    return sid, frames


def test_render_drift_png_from_series(project):
    sid, _frames = _drift_project(project)
    series = drift_series(project, sid)
    assert series.n_solved == 3 and len(series.points) == 3
    png = render_drift_png(series)
    assert png.startswith(PNG_MAGIC)
    with pytest.raises(DriftError):
        render_drift_png(series, width_px=0)
    with pytest.raises(DriftError):
        render_drift_png(series, height_px=99999)


def test_render_drift_png_empty_series_still_renders():
    series = DriftSeries(
        session_id="s1",
        requested_ra_deg=REQ_RA_DEG,
        requested_dec_deg=REQ_DEC_DEG,
        points=(),
        n_frames=0,
        n_solved=0,
        n_missing_wcs=0,
        ra_drift_arcsec_per_hour=None,
        dec_drift_arcsec_per_hour=None,
    )
    assert render_drift_png(series).startswith(PNG_MAGIC)


# --- SolveController (pure, direct invoke) ---------------------------------


class FakeViewer:
    """A duck-typed stand-in for the shared viewer controller."""

    def __init__(self, project, frame_id=None, label="stack label"):
        self.project = project
        self._listeners = []
        self.snapshot = SimpleNamespace(
            current=(
                SimpleNamespace(frame_id=frame_id, label=lambda: label)
                if frame_id is not None
                else None
            )
        )

    def add_listener(self, listener):
        self._listeners.append(listener)

    def remove_listener(self, listener):
        if listener in self._listeners:
            self._listeners.remove(listener)

    def emit(self):
        for listener in list(self._listeners):
            listener(self.snapshot)


def _fake_result() -> SolveResult:
    return SolveResult(
        wcs=None,
        cards=dict(CARDS),
        solver="gaia",
        solved_at="2026-11-20T22:05:00",
        center_ra_deg=REQ_RA_DEG,
        center_dec_deg=REQ_DEC_DEG,
        n_stars=5,
        rms_arcsec=0.4,
        matches=(),
        rows=(),
    )


def test_solve_controller_with_project_and_viewer_keeps_the_open_stack(
    project, plain_stack_frame
):
    """Regression: constructing with BOTH ``project=`` and
    ``viewer_controller=`` must keep the viewer's open stack (set_project
    clears the stack state, so it must run before attach_viewer adopts it)."""
    frame = plain_stack_frame
    viewer = FakeViewer(project, frame_id=frame.id)
    controller = SolveController(project=project, viewer_controller=viewer)
    snap = controller.snapshot
    assert snap.project_name == "M 42"
    assert snap.stack_frame_id == frame.id
    assert snap.stack_label == "stack label"
    # a later viewer change still updates the adopted stack
    viewer.snapshot = SimpleNamespace(current=None)
    viewer.emit()
    assert controller.snapshot.stack_frame_id is None


def test_solve_controller_follows_viewer_and_solves(project, plain_stack_frame, monkeypatch):
    frame = plain_stack_frame
    viewer = FakeViewer(project, frame_id=frame.id)
    controller = SolveController(viewer_controller=viewer)
    snap = controller.snapshot
    assert snap.project_name == "M 42"
    assert snap.stack_frame_id == frame.id
    assert snap.stack_label == "stack label"
    assert snap.solved is None

    monkeypatch.setattr(
        astrometry_session, "solve_frame", lambda **kwargs: _fake_result()
    )
    snap = controller.solve_open_stack()
    assert snap.error is None
    assert snap.solving is False
    assert snap.solved is not None and snap.solved.solver == "gaia"
    assert snap.last_solver == "gaia"
    assert snap.last_n_stars == 5
    assert snap.status is not None and "solved" in snap.status
    # persisted via store.wcs AND written into the FITS header
    assert load_frame_wcs(project, frame.id) is not None
    with pyfits.open(io.BytesIO(read_frame_bytes(project, frame))) as hdul:
        assert hdul[0].header["CTYPE1"] == "RA---TAN"


def test_solve_controller_failure_is_recorded_not_raised(project, plain_stack_frame, monkeypatch):
    frame = plain_stack_frame
    controller = SolveController(project=project)
    controller._adopt_stack(frame.id, "lbl")

    def boom(**kwargs):
        raise SolveError("boom: no stars")

    monkeypatch.setattr(astrometry_session, "solve_frame", boom)
    snap = controller.solve_open_stack()
    assert snap.error is not None and "boom" in snap.error
    assert snap.solved is None
    assert snap.status is None
    # nothing was written
    assert load_frame_wcs(project, frame.id) is None
    with pyfits.open(io.BytesIO(read_frame_bytes(project, frame))) as hdul:
        assert "CTYPE1" not in hdul[0].header


def test_solve_controller_without_stack_reports_error(project):
    controller = SolveController(project=project)
    snap = controller.solve_open_stack()
    assert snap.error is not None and "no stack open" in snap.error
    with pytest.raises(AstrometryError):
        controller.default_export_path("/tmp")


def test_solve_controller_revives_a_closed_handle_after_reopen(
    project, plain_stack_frame, monkeypatch
):
    """Issue 08 regression: the controller keeps the OLD Project handle; that
    handle's db is closed (night stopped) and the same path is re-opened in
    the Projects view (a NEW Project, because the old handle was closed). The
    next solve_open_stack must not hit ``sqlite3.ProgrammingError: Cannot
    operate on a closed database`` — the ``_call_store`` path revives the old
    handle via :meth:`Project.ensure_open`."""
    frame = plain_stack_frame
    controller = SolveController(project=project)
    controller._adopt_stack(frame.id, "lbl")
    monkeypatch.setattr(
        astrometry_session, "solve_frame", lambda **kwargs: _fake_result()
    )
    # night stopped: the handle the controller holds is closed; re-opening the
    # same path yields a NEW handle (the closed one is not reused)
    project.close()
    reopened = open_project(project.path)
    assert reopened is not project
    snap = controller.solve_open_stack()
    assert snap.error is None  # no ProgrammingError
    assert snap.solved is not None and snap.solved.solver == "gaia"
    assert load_frame_wcs(project, frame.id) is not None  # the revived handle works
    # ... and the same through the marshalled (non-db-thread) _call_store branch
    controller._db_thread_id = -1  # pretend the db is owned by another thread
    snap = controller.solve_open_stack()
    assert snap.error is None
    assert snap.solved is not None
    reopened.close()
    project.close()  # idempotent


def test_solve_controller_export_and_listeners(project, stack_frame, tmp_path):
    frame, _header = stack_frame
    controller = SolveController(project=project)
    controller._adopt_stack(frame.id, "lbl")
    seen = []
    controller.add_listener(seen.append)
    out = tmp_path / "annotated.png"
    snap = controller.export_annotated_png(out)
    assert snap.error is None
    assert snap.status is not None and snap.status.startswith("exported")
    assert out.read_bytes().startswith(PNG_MAGIC)
    assert seen, "listener was notified"
    assert controller.default_export_path(tmp_path).name.startswith("annotated-")
    controller.remove_listener(seen.append)
    count = len(seen)
    controller.refresh_solved()
    assert len(seen) == count


# --- DriftController (pure, direct invoke) ---------------------------------


def test_drift_controller_lists_sessions_and_refreshes(project):
    sid, _frames = _drift_project(project)
    controller = DriftController(project=project)
    snap = controller.snapshot
    assert len(snap.sessions) == 1
    assert snap.session_id == sid  # the open session is auto-selected
    with pytest.raises(DriftError):
        controller.render_png()  # no series yet
    snap = controller.refresh()
    assert snap.error is None
    assert snap.series is not None and len(snap.series.points) == 3
    assert snap.status is not None and "solved frames" in snap.status
    assert controller.render_png().startswith(PNG_MAGIC)


def test_drift_controller_unsolved_and_unknown_sessions(project):
    sid, _frames = _drift_project(project)
    # a second session without any solved frame
    close_session(project, sid)
    sid2 = start_session(project, started_at=T0 + timedelta(hours=2))
    hdu = pyfits.PrimaryHDU(data=[[1.0]])
    hdu.header["DATE-OBS"] = (T0 + timedelta(hours=2)).isoformat(timespec="milliseconds")
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    store_frame(project, sid2, KIND_SUB, buf.getvalue())

    controller = DriftController(project=project)
    snap = controller.set_session(sid2)
    assert snap.session_id == sid2 and snap.series is None
    snap = controller.refresh()
    assert snap.series is None and snap.error is not None

    snap = controller.set_session("no-such-session")
    snap = controller.refresh()
    assert snap.error is not None and "SessionNotFoundError" in snap.error

    snap = controller.set_session(None)
    assert snap.session_id is None
    snap = controller.refresh()
    assert snap.error is not None and "no session" in snap.error


def test_drift_controller_follows_viewer_project(project):
    _drift_project(project)
    viewer = FakeViewer(project)
    controller = DriftController(viewer_controller=viewer)
    assert controller.project is project
    assert len(controller.snapshot.sessions) == 1
    viewer.project = None
    viewer.emit()
    # an explicit follow keeps the project (only adopted when unset)
    assert controller.project is project
