"""Pure-layer tests for photometry ticket 04 — the run + results controller
(:mod:`seestar_controller.photometry.run`).

Headless: no ``gi`` anywhere (the sandbox has no GTK, see
docs/wiki/assumptions.md). The runs execute on SYNTHETIC FITS sub-frames with
known injected fluxes (the same helpers as tests/test_photometry_measure.py:
3 comps at constant flux ``F0`` and a target whose flux varies sinusoidally,
so the injected differential magnitude of frame ``i`` is
``m_i = -2.5*log10(F_target_i / F0)``), stored in a tmp project via
:mod:`seestar_controller.store`.

Covered: :meth:`RunController.run_session` over ALL sub-frames of a session
(measurements appended to the project lightcurve with jd/mag/mag_err, stacks
and other sessions never measured, the ``on_progress`` sequence ``1..N``,
skipped frames collected, the frozen :class:`RunReport`), the picks resolution
(shared picker controller first, stored picks as fallback), the error paths
(no project, unknown session, no target pick, bad ensemble, re-entrant run),
the listener snapshots (progress ticks + lightcurve), the worker-thread safety
(store calls marshalled through ``invoke_on_main`` to the db-owning thread),
the results accessor (:meth:`RunController.sessions` /
:meth:`RunController.lightcurve_points` / :meth:`RunController.set_excluded`
outlier flagging reflected in the lightcurve), and the package boundary
(subprocess + source-scan, mirroring tests/test_photometry_picking.py).
"""

import io
import math
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits as pyfits
from astropy.time import Time

import seestar_controller.photometry as photometry
from seestar_controller.photometry import run as run_module
from seestar_controller.photometry import (
    ApertureParams,
    PhotometryError,
    PickController,
    StarPickSpec,
)
from seestar_controller.photometry.run import (
    DB_CALL_TIMEOUT_S,
    RunController,
    RunError,
    RunProgress,
    RunReport,
    RunSnapshot,
)
from seestar_controller.store import (
    KIND_STACK,
    KIND_SUB,
    StarPick,
    close_session,
    create_project,
    lightcurve,
    list_frames,
    list_sessions,
    load_star_picks,
    measurements_for_frame,
    save_star_picks,
    set_excluded as store_set_excluded,
    start_session,
    store_frame,
)
from seestar_controller.store.errors import (
    MeasurementError as StoreMeasurementError,
)
from seestar_controller.store.measurements import Measurement

#: Directory of the photometry package (source-scan audit).
PHOTOMETRY_PACKAGE_DIR = Path(photometry.__file__).resolve().parent

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=timezone.utc)

# --- synthetic frames with known injected fluxes (same as test_photometry_measure.py) -------------

SIZE = 64
BACKGROUND = 100.0
NOISE = 5.0
F0 = 20000.0  # constant comp peak flux
#: Star positions as (cy, cx) — row, column.
TARGET_POS = (30, 40)
COMP_POSITIONS = ((16, 16), (12, 50), (50, 12))

#: The aperture used everywhere (the defaults).
APERTURE = ApertureParams(radius_px=4.0, sky_in_px=8.0, sky_out_px=12.0)


def date_obs_str(i: int, seconds: int = 10) -> str:
    """The DATE-OBS of frame ``i``: T0 + i*seconds, naive ISOT (UTC)."""
    instant = T0 + timedelta(seconds=seconds * i)
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "")


def expected_jd(i: int) -> float:
    return float(Time(date_obs_str(i), format="isot", scale="utc").jd)


def frame_data(target_peak: float, seed: int) -> np.ndarray:
    """A synthetic 64x64 frame: flat background + Gaussian stars (sigma^2=4)
    + noise, float32. The target sits at TARGET_POS with ``target_peak``, the
    3 comps at COMP_POSITIONS with constant peak ``F0``."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    data = np.full((SIZE, SIZE), BACKGROUND, dtype=np.float64)
    stars = [(TARGET_POS[0], TARGET_POS[1], target_peak)] + [
        (cy, cx, F0) for cy, cx in COMP_POSITIONS
    ]
    for cy, cx, peak in stars:
        data = data + peak * np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / 8.0))
    data = data + rng.normal(0.0, NOISE, size=(SIZE, SIZE))
    return np.ascontiguousarray(data, dtype=np.float32)


def fits_bytes(data: np.ndarray, *, date_obs: str | None = "2026-11-20T22:00:00.000") -> bytes:
    """A small valid FITS file with the given data (+ DATE-OBS unless None)."""
    hdu = pyfits.PrimaryHDU(data=np.asarray(data, dtype=np.float32))
    if date_obs is not None:
        hdu.header["DATE-OBS"] = date_obs
    hdu.header["EXPTIME"] = 10.0
    hdu.header["GAIN"] = 50
    hdu.header["OBJECT"] = "M 42"
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def make_picks(target_xy: tuple[float, float] = (40.0, 30.0)) -> list[StarPickSpec]:
    """One target + 3 comps at the synthetic star positions (x=column, y=row)."""
    return [
        StarPickSpec(role="target", x=target_xy[0], y=target_xy[1], label="Target"),
        StarPickSpec(role="comp", x=16.0, y=16.0, label="Comp A"),
        StarPickSpec(role="comp", x=50.0, y=12.0, label="Comp B"),
        StarPickSpec(role="comp", x=12.0, y=50.0, label="Comp C"),
    ]


def store_picks(project) -> list[StarPick]:
    """Persist the synthetic picks into the project as store StarPick records."""
    stored = [
        StarPick(
            role=p.role, label=p.label, x=p.x, y=p.y, ra_deg=None, dec_deg=None,
            aperture_radius=4.0, sky_annulus_inner=8.0, sky_annulus_outer=12.0,
        )
        for p in make_picks()
    ]
    save_star_picks(project, stored)
    return stored


def wcs_header() -> dict:
    """A plain-dict FITS header with a simple CAR WCS (astropy.wcs.WCS)."""
    from astropy.wcs import WCS

    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---CAR", "DEC--CAR"]
    w.wcs.crval = [83.0, 22.0]
    w.wcs.crpix = [33.0, 33.0]
    w.wcs.cdelt = [-0.001, 0.001]
    return dict(w.to_header())


class FakeImage:
    """A duck-typed FitsImage stand-in (``data`` / ``header`` / ``width`` /
    ``height``) — the pure layer never imports enhance."""

    def __init__(self, data: np.ndarray, header: dict) -> None:
        self.data = np.asarray(data, dtype=np.float32)
        self.header = header

    @property
    def width(self) -> int:
        return int(self.data.shape[1])

    @property
    def height(self) -> int:
        return int(self.data.shape[0])


def frozen(dt: datetime):
    return lambda: dt


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    p = create_project("M 42 night", root=root)
    yield p
    p.close()


def make_session(project, n: int = 8, amp: float = 0.3, *, clock=None, with_stack: bool = True):
    """One session with ``n`` synthetic sub-frames (sinusoidal target) plus
    optionally a STACK frame (which must never be measured). Returns
    ``(session_id, frames, injected_mags)``. Any still-open session is closed
    first (the store allows only one open session at a time)."""
    for open_session in list_sessions(project):
        if open_session.ended_at is None:
            close_session(project, open_session.id)
    session = start_session(project, clock=clock or frozen(T0))
    injected: list[float] = []
    frames = []
    for i in range(n):
        m_inj = amp * math.sin(2.0 * math.pi * i / n)
        injected.append(m_inj)
        target_peak = F0 * 10 ** (-0.4 * m_inj)
        frame = store_frame(
            project,
            session,
            KIND_SUB,
            fits_bytes(frame_data(target_peak, seed=100 + i), date_obs=date_obs_str(i)),
        )
        frames.append(frame)
    if with_stack:
        store_frame(
            project,
            session,
            KIND_STACK,
            fits_bytes(frame_data(F0 * 2.0, seed=999), date_obs=date_obs_str(50)),
        )
    return session, frames, injected


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


# --- run_session: happy path — all subs measured, lightcurve appended, progress 1..N ----------------


def test_run_session_measures_all_subs_appends_lightcurve_and_reports_progress(project):
    session, frames, injected = make_session(project, n=8)
    # a second session whose frames must NOT be measured by this run
    other_session, _, _ = make_session(
        project, n=2, clock=frozen(T0 + timedelta(hours=1))
    )
    store_picks(project)
    ctrl = RunController(project=project)

    progress: list[tuple[int, int, int]] = []
    report = ctrl.run_session(
        session, on_progress=lambda done, total, frame: progress.append((done, total, frame.id))
    )

    assert isinstance(report, RunReport)
    assert report.session_id == session
    assert len(report.measurements) == 8
    assert report.skipped == ()
    assert report.frames == 8  # only the sub-frames — never the stack
    # the progress callback fired after each frame: done = 1..N, total = N
    assert [d for d, _t, _f in progress] == list(range(1, 9))
    assert all(t == 8 for _d, t, _f in progress)
    assert [f for _d, _t, f in progress] == [f.id for f in frames]
    # every measurement was appended to the project lightcurve (jd, mag, mag_err)
    lc = lightcurve(project)
    assert len(lc) == 8  # the other session's 2 frames are not in this run
    assert [m.frame_id for m in lc] == [f.id for f in frames]
    assert [m.id for m in report.measurements] == [m.id for m in lc]
    for meas, m_inj, i in zip(lc, injected, range(8)):
        assert isinstance(meas, Measurement)
        assert meas.session_id == session
        assert meas.mag == pytest.approx(m_inj, abs=0.02)  # injected diff mag recovered
        assert meas.mag_err is not None and meas.mag_err > 0.0
        assert meas.jd == pytest.approx(expected_jd(i), abs=1e-9)  # from DATE-OBS
        assert not meas.excluded
        assert measurements_for_frame(project, meas.frame_id) == [meas]
    # the stack frame was never measured
    stack_frames = list_frames(project, session_id=session, kind=KIND_STACK)
    assert len(stack_frames) == 1
    assert measurements_for_frame(project, stack_frames[0].id) == []
    # the other session's frames were not measured either
    assert len(list_frames(project, session_id=other_session, kind=KIND_SUB)) == 2
    assert all(m.session_id == session for m in lightcurve(project, include_excluded=True))
    # the controller state after the run
    assert ctrl.running is False
    assert ctrl.session_id == session
    assert ctrl.report is report
    assert ctrl.error is None
    snap = ctrl.snapshot
    assert isinstance(snap, RunSnapshot)
    assert not snap.running and snap.report is report
    assert len(snap.lightcurve) == 8
    assert ctrl.lightcurve_points() == tuple(lc)
    assert ctrl.lightcurve_points(include_excluded=True) == tuple(lc)


def test_run_session_collects_skipped_frames_without_crashing(project):
    session = start_session(project, clock=frozen(T0))
    good = store_frame(
        project, session, KIND_SUB,
        fits_bytes(frame_data(F0, seed=1), date_obs=date_obs_str(0)),
    )
    # an all-zero frame: background-subtracted flux is exactly 0 -> skipped
    zero = store_frame(
        project, session, KIND_SUB,
        fits_bytes(np.zeros((SIZE, SIZE), dtype=np.float32), date_obs=date_obs_str(1)),
    )
    store_frame(
        project, session, KIND_SUB,
        fits_bytes(frame_data(F0, seed=3), date_obs=date_obs_str(2)),
    )
    store_picks(project)
    ctrl = RunController(project=project)
    progress: list[int] = []
    report = ctrl.run_session(session, on_progress=lambda d, t, f: progress.append(d))
    assert len(report.measurements) == 2
    assert [m.frame_id for m in report.measurements] == [good.id, report.measurements[1].frame_id]
    assert len(report.skipped) == 1
    assert report.skipped[0].frame_id == zero.id
    assert "non-positive" in report.skipped[0].reason
    assert report.frames == 3
    assert progress == [1, 2, 3]  # progress advances even for skipped frames
    assert len(lightcurve(project)) == 2


def test_run_session_empty_session_is_a_valid_empty_run(project):
    session = start_session(project, clock=frozen(T0))
    store_picks(project)
    ctrl = RunController(project=project)
    progress: list[int] = []
    report = ctrl.run_session(session, on_progress=lambda d, t, f: progress.append(d))
    assert report.measurements == () and report.skipped == ()
    assert report.session_id == session and report.frames == 0
    assert progress == []
    assert lightcurve(project) == []
    assert ctrl.report is report


# --- picks resolution: picker controller first, stored picks as fallback -----------------------------


def test_run_session_uses_the_picker_controllers_picks(project):
    session, _frames, injected = make_session(project, n=4)
    # NO stored picks — the shared picker controller's picks are used
    assert load_star_picks(project) == []
    picker = PickController(project=project)
    picker.set_image(FakeImage(frame_data(F0, seed=77), wcs_header()))
    picker.pick_at(40.0, 30.0, "target", label="Target", snap=False)
    picker.pick_at(16.0, 16.0, "comp", label="Comp A", snap=False)
    picker.pick_at(50.0, 12.0, "comp", label="Comp B", snap=False)
    picker.pick_at(12.0, 50.0, "comp", label="Comp C", snap=False)
    assert len(picker.picks) == 4
    ctrl = RunController(picker_controller=picker)  # adopts the picker's project
    assert ctrl.project is project
    report = ctrl.run_session(session)
    assert len(report.measurements) == 4 and report.skipped == ()
    for meas, m_inj in zip(report.measurements, injected):
        assert meas.mag == pytest.approx(m_inj, abs=0.02)


def test_run_session_falls_back_to_stored_picks_when_picker_is_empty(project):
    session, _frames, injected = make_session(project, n=3)
    store_picks(project)
    picker = PickController(project=project)  # loads the stored picks... clear them
    picker.clear()
    assert picker.picks == ()
    ctrl = RunController(picker_controller=picker)
    report = ctrl.run_session(session)
    assert len(report.measurements) == 3
    for meas, m_inj in zip(report.measurements, injected):
        assert meas.mag == pytest.approx(m_inj, abs=0.02)


# --- error paths --------------------------------------------------------------------------------------


def test_run_session_requires_a_project():
    ctrl = RunController()
    with pytest.raises(RunError, match="no project"):
        ctrl.run_session("whatever")
    with pytest.raises(RunError, match="no project"):
        ctrl.sessions()
    with pytest.raises(RunError, match="no project"):
        ctrl.lightcurve_points()
    with pytest.raises(RunError, match="no project"):
        ctrl.set_excluded(1, True)


def test_run_session_unknown_session_raises_naming_known_sessions(project):
    session, _frames, _inj = make_session(project, n=2)
    store_picks(project)
    ctrl = RunController(project=project)
    with pytest.raises(RunError, match="no session 'nope'"):
        ctrl.run_session("nope")
    assert session in str(ctrl.error)  # the known sessions are named
    assert ctrl.running is False
    assert lightcurve(project) == []  # nothing was measured
    # a valid run still works afterwards
    report = ctrl.run_session(session)
    assert len(report.measurements) == 2


def test_run_session_validates_arguments(project):
    store_picks(project)
    ctrl = RunController(project=project)
    for bad in ("", None, 123, True):
        with pytest.raises(RunError, match="session_id"):
            ctrl.run_session(bad)  # type: ignore[arg-type]
    with pytest.raises(RunError, match="on_progress"):
        ctrl.run_session("x", on_progress="nope")  # type: ignore[arg-type]


def test_run_session_no_target_pick_raises_before_measuring(project):
    session, _frames, _inj = make_session(project, n=2)
    # no stored picks, no picker picks
    ctrl = RunController(project=project)
    with pytest.raises(RunError, match="no target pick"):
        ctrl.run_session(session)
    assert lightcurve(project) == []
    assert ctrl.error is not None and "no target pick" in ctrl.error
    # comps only (via the picker controller — the store refuses to save such
    # picks) -> also unrunnable
    picker = PickController(project=project)
    picker.set_image(FakeImage(frame_data(F0, seed=5), wcs_header()))
    picker.pick_at(16.0, 16.0, "comp", label="Comp A", snap=False)
    ctrl2 = RunController(picker_controller=picker)
    with pytest.raises(RunError, match="exactly one"):
        ctrl2.run_session(session)
    assert lightcurve(project) == []


def test_run_session_rejects_a_concurrent_run(project):
    session, _frames, _inj = make_session(project, n=3)
    store_picks(project)
    ctrl = RunController(project=project)
    seen: list[int] = []

    def on_progress(done, total, frame):
        seen.append(done)
        if done == 1:
            # re-entrant call from inside the progress callback is rejected
            with pytest.raises(RunError, match="already in progress"):
                ctrl.run_session(session)

    report = ctrl.run_session(session, on_progress=on_progress)
    assert seen == [1, 2, 3]  # the outer run was not disturbed by the rejected inner one
    assert len(report.measurements) == 3
    assert ctrl.running is False  # the finally block cleared it
    # and the controller is usable again
    report = ctrl.run_session(session)
    assert len(report.measurements) == 3


# --- listeners + snapshots ------------------------------------------------------------------------------


def test_listeners_receive_snapshots_with_progress_report_and_lightcurve(project):
    session, frames, _inj = make_session(project, n=4)
    store_picks(project)
    ctrl = RunController(project=project)  # default invoke_on_main: direct
    snapshots: list[RunSnapshot] = []
    ctrl.add_listener(snapshots.append)
    assert ctrl.listeners == (snapshots.append,)
    ctrl.run_session(session)
    assert len(snapshots) >= 2 + 4  # start + one per frame + end
    assert snapshots[0].running is True and snapshots[0].session_id == session
    assert snapshots[0].report is None
    # progress ticks were emitted while running
    ticks = [s.progress for s in snapshots if s.running and s.progress is not None]
    assert [t.done for t in ticks] == [1, 2, 3, 4]
    assert all(isinstance(t, RunProgress) and t.total == 4 for t in ticks)
    assert all(t.frame_id == f.id for t, f in zip(ticks, frames))
    last = snapshots[-1]
    assert last.running is False
    assert isinstance(last.report, RunReport) and len(last.report.measurements) == 4
    assert len(last.lightcurve) == 4
    assert last.error is None
    # remove_listener stops the notifications
    ctrl.remove_listener(snapshots.append)
    assert ctrl.listeners == ()
    n = len(snapshots)
    ctrl.set_excluded(last.lightcurve[0].id, True)
    assert len(snapshots) == n


def test_failing_listener_is_isolated(project):
    session, _frames, _inj = make_session(project, n=2)
    store_picks(project)
    ctrl = RunController(project=project)
    good: list[RunSnapshot] = []

    def broken(_snapshot):
        raise RuntimeError("boom")

    ctrl.add_listener(broken)
    ctrl.add_listener(good.append)
    report = ctrl.run_session(session)  # must not raise
    assert len(report.measurements) == 2
    assert good  # the good listener still got its snapshots


def test_queued_invoke_on_main_marshals_listener_notifications(project):
    session, _frames, _inj = make_session(project, n=2)
    store_picks(project)
    queued = QueuedInvoke()
    ctrl = RunController(project=project, invoke_on_main=queued)
    snapshots: list[RunSnapshot] = []
    ctrl.add_listener(snapshots.append)
    ctrl.run_session(session)  # same thread: store calls run directly
    assert snapshots == []  # notifications are queued, not run yet
    queued.drain()
    assert len(snapshots) >= 3
    assert snapshots[-1].running is False and snapshots[-1].report is not None


# --- worker-thread safety: store calls marshalled to the db-owning thread -------------------------------


def test_run_session_from_a_worker_thread_marshals_store_calls(project):
    """The project db lives on the main thread; run_session runs on a worker
    thread — every store call is marshalled through invoke_on_main (pumped by
    the main thread here, GLib.idle_add in the app) and the FITS measuring
    stays on the worker."""
    session, frames, injected = make_session(project, n=4)
    store_picks(project)
    queued = QueuedInvoke()
    ctrl = RunController(project=project, invoke_on_main=queued)
    snapshots: list[RunSnapshot] = []
    ctrl.add_listener(snapshots.append)

    progress_threads: list[int] = []
    result: dict = {}

    def on_progress(done, total, frame):
        progress_threads.append(threading.get_ident())  # the worker thread

    def work():
        result["report"] = ctrl.run_session(session, on_progress=on_progress)

    worker = threading.Thread(target=work, name="test-photometry-run", daemon=True)
    worker.start()
    main_id = threading.get_ident()
    deadline = time.monotonic() + 60.0
    while worker.is_alive() and time.monotonic() < deadline:
        queued.pump_one(timeout=0.05)  # the "main loop" runs the store calls
    worker.join(timeout=10.0)
    assert not worker.is_alive()
    queued.drain()  # the trailing notifications

    report = result["report"]
    assert isinstance(report, RunReport) and len(report.measurements) == 4
    assert report.skipped == ()
    # the progress callback ran on the WORKER thread, not the main thread
    assert progress_threads and all(t == worker.ident for t in progress_threads)
    assert worker.ident != main_id
    # the measurements really landed in the db (read from the main thread)
    lc = lightcurve(project)
    assert [m.id for m in lc] == [m.id for m in report.measurements]
    for meas, m_inj in zip(lc, injected):
        assert meas.mag == pytest.approx(m_inj, abs=0.02)
    # the listener notifications were marshalled and delivered
    assert snapshots and snapshots[-1].running is False
    assert len(snapshots[-1].lightcurve) == 4


def test_run_session_on_a_worker_thread_that_owns_the_db(project_rootless=None):
    """When the project (and controller) are created ON the worker thread,
    run_session needs no marshalling at all — it is safe to call directly."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "projects"
        result: dict = {}

        def work():
            p = create_project("worker night", root=root)
            try:
                session, _frames, _inj = make_session(p, n=3)
                store_picks(p)
                ctrl = RunController(project=p)  # db thread = this worker thread
                result["report"] = ctrl.run_session(session)
                result["lc"] = lightcurve(p)
            finally:
                p.close()

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        worker.join(timeout=60.0)
        assert not worker.is_alive()
    report = result["report"]
    assert len(report.measurements) == 3 and report.skipped == ()
    assert len(result["lc"]) == 3


# --- results accessor: sessions, lightcurve points, outlier exclusion -------------------------------------


def test_sessions_lists_the_project_sessions(project):
    session1, _, _ = make_session(project, n=1)
    session2, _, _ = make_session(project, n=1, clock=frozen(T0 + timedelta(hours=2)))
    ctrl = RunController(project=project)
    ids = [s.id for s in ctrl.sessions()]
    assert ids == [session1, session2] or set(ids) == {session1, session2}


def test_set_excluded_flags_outliers_and_is_reflected_in_the_lightcurve(project):
    session, _frames, _inj = make_session(project, n=4)
    store_picks(project)
    ctrl = RunController(project=project)
    snapshots: list[RunSnapshot] = []
    ctrl.add_listener(snapshots.append)
    report = ctrl.run_session(session)
    assert len(ctrl.lightcurve_points()) == 4
    assert len(ctrl.lightcurve_points(include_excluded=False)) == 4

    victim = report.measurements[1]
    updated = ctrl.set_excluded(victim.id, True)
    assert updated.id == victim.id and updated.excluded is True
    # the store lightcurve reflects the flag both ways
    assert victim.id not in [m.id for m in lightcurve(project)]
    assert victim.id in [m.id for m in lightcurve(project, include_excluded=True)]
    assert store_set_excluded(project, victim.id, True).excluded is True
    # the controller accessor: excluded points included by default, filtered on demand
    assert victim.id in [m.id for m in ctrl.lightcurve_points()]
    assert victim.id in [m.id for m in ctrl.lightcurve_points(include_excluded=True)]
    assert victim.id not in [m.id for m in ctrl.lightcurve_points(include_excluded=False)]
    # the snapshot carries the full lightcurve (incl. excluded) + a notification fired
    snap = ctrl.snapshot
    assert victim.id in [m.id for m in snap.lightcurve]
    assert snapshots and snapshots[-1].lightcurve == snap.lightcurve
    # unflag again
    ctrl.set_excluded(victim.id, False)
    assert victim.id in [m.id for m in lightcurve(project)]
    assert victim.id in [m.id for m in ctrl.lightcurve_points(include_excluded=False)]


def test_set_excluded_error_paths(project):
    session, _frames, _inj = make_session(project, n=1)
    store_picks(project)
    ctrl = RunController(project=project)
    report = ctrl.run_session(session)
    mid = report.measurements[0].id
    with pytest.raises(RunError, match="no measurement"):
        ctrl.set_excluded(9999, True)
    with pytest.raises(RunError, match="excluded must be a bool"):
        ctrl.set_excluded(mid, "yes")  # type: ignore[arg-type]
    with pytest.raises(RunError, match="measurement_id must be an int"):
        ctrl.set_excluded("1", True)  # type: ignore[arg-type]
    # the flag is unchanged after the failed calls
    assert lightcurve(project)[0].excluded is False


# --- package boundary (subprocess + source scan) + public API ---------------------------------------------


def test_run_module_imports_no_gi_no_link_no_ui_no_enhance_and_widget_stays_lazy():
    code = (
        "import sys;"
        "import seestar_controller.photometry;"
        "import seestar_controller.photometry.run;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported eagerly';"
        "assert 'seestar_controller.enhance' not in sys.modules, 'enhance imported eagerly';"
        "assert 'seestar_controller.photometry.results_view' not in sys.modules, "
        "'results_view imported eagerly';"
        "assert 'seestar_controller.photometry.star_picker' not in sys.modules, "
        "'star_picker imported eagerly';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_run_module_source_scan_no_gi_no_widget_import_and_package_module_set():
    source = (PHOTOMETRY_PACKAGE_DIR / "run.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), (
        "pure module run imports gi"
    )
    assert not re.search(r"^\s*(from|import)\s+.*(results_view|star_picker|suggestions_panel)",
                         source, re.MULTILINE), "pure module run imports a widget module"
    assert not re.search(r"^\s*(from|import)\s+.*\b(ui|link|enhance)\b", source, re.MULTILINE), (
        "pure module run imports ui/link/enhance"
    )
    modules = {path.stem for path in PHOTOMETRY_PACKAGE_DIR.glob("*.py")}
    assert modules == {
        "__init__",
        "picking",
        "suggest",
        "measure",
        "run",
        "star_picker",
        "suggestions_panel",
        "results_view",
    }


def test_run_public_api_is_exported_from_package():
    for name in (
        "DB_CALL_TIMEOUT_S",
        "ProgressCallback",
        "RunController",
        "RunError",
        "RunListener",
        "RunProgress",
        "RunReport",
        "RunSnapshot",
    ):
        assert name in photometry.__all__, name
        assert hasattr(photometry, name), name
    assert "ResultsPanel" in photometry.__all__  # lazy widget export
    assert photometry.RunController is run_module.RunController
    assert photometry.RunError is run_module.RunError
    assert issubclass(photometry.RunError, photometry.PhotometryError)
    assert issubclass(photometry.RunError, ValueError)
    # deliberately distinct from the store's MeasurementError
    assert not issubclass(photometry.RunError, StoreMeasurementError)
    assert DB_CALL_TIMEOUT_S > 0
    # the frozen dataclasses
    prog = RunProgress(done=1, total=2, frame_id=7)
    assert (prog.done, prog.total, prog.frame_id) == (1, 2, 7)
    with pytest.raises(Exception):
        prog.done = 5  # type: ignore[misc]
