"""Pure-layer tests for issue 10 — the RunController side of the batch
plate-solve of a session's unsolved subs
(:meth:`seestar_controller.photometry.run.RunController.solve_session_unsolved`
and :meth:`~.RunController.wcs_coverage`).

Headless: no ``gi`` anywhere. The plate solver is MOCKED
(``seestar_controller.astrometry.session.solve_frame`` is monkeypatched — no
solve-field subprocess, no Gaia network); the frames are real stored FITS
subs, so the whole store side (frame listing, ``frame_wcs`` upsert, WCS cards
written into the stored FITS) runs for real.

Covered: the happy path (frozen report, progress ``1..N`` on the calling
thread, db records, ``solve_report`` / ``solving`` / ``solve_error`` state),
per-frame :class:`~seestar_controller.astrometry.SolveError` tolerance, the
retry semantics of a still-unsolved sub, solver-kwarg forwarding, the error
paths (no project, bad session id, unknown session naming the known ones),
the run-or-solve busy guard, the worker-thread safety (every store call
marshalled through ``invoke_on_main`` to the db-owning thread while the slow
solve stays on the worker), :meth:`RunController.wcs_coverage` (subs only,
error paths), and the package boundary (subprocess: importing the pure
photometry layer pulls in neither ``gi`` nor ``astropy``).
"""

from __future__ import annotations

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

from seestar_controller import photometry
from seestar_controller.astrometry import SolveError
from seestar_controller.astrometry import session as astrometry_session
from seestar_controller.astrometry.solve import SolveResult
from seestar_controller.photometry.run import RunController, RunError
from seestar_controller.store import (
    KIND_STACK,
    KIND_SUB,
    create_project,
    save_frame_wcs,
    start_session,
    store_frame,
)
from seestar_controller.store.wcs import list_solved_frames

#: Directory of the photometry package (source-scan audit).
PHOTOMETRY_PACKAGE_DIR = Path(photometry.__file__).resolve().parent

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)

#: A valid TAN WCS card set.
CARDS = {
    "CTYPE1": "RA---TAN",
    "CTYPE2": "DEC--TAN",
    "CRVAL1": 83.0,
    "CRVAL2": 22.0,
    "CRPIX1": 8.5,
    "CRPIX2": 8.5,
    "CDELT1": -0.001,
    "CDELT2": 0.001,
}


# --- fixtures + helpers ---------------------------------------------------------------------


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    p = create_project("M 42 night", root=root)
    yield p
    p.close()


def frozen(dt: datetime):
    return lambda: dt


def sub_fits(i: int) -> bytes:
    """A small valid FITS sub with a per-index DATE-OBS (T0 + i*10s)."""
    import io

    rng = np.random.default_rng(100 + i)
    data = np.ascontiguousarray(
        np.full((16, 16), 100.0) + rng.normal(0.0, 2.0, (16, 16)), dtype=np.float32
    )
    hdu = pyfits.PrimaryHDU(data=data)
    hdu.header["DATE-OBS"] = (T0 + timedelta(seconds=10 * i)).isoformat(timespec="milliseconds")
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def sub_index(header) -> int:
    """The sub index encoded in the DATE-OBS card (seconds offset / 10)."""
    return (int(datetime.fromisoformat(header["DATE-OBS"]).timestamp() - T0.timestamp()) // 10)


def make_session(project, n: int) -> tuple[str, list]:
    """One session with ``n`` sub-frames; returns ``(session_id, frames)`` in
    jd order."""
    sid = start_session(project, clock=frozen(T0))
    frames = [store_frame(project, sid, KIND_SUB, sub_fits(i)) for i in range(n)]
    return sid, frames


def fake_solve_result(index: int) -> SolveResult:
    return SolveResult(
        wcs=None,
        cards=dict(CARDS),
        solver="gaia",
        solved_at=f"2026-11-22T00:00:{index:02d}.000+00:00",
        center_ra_deg=83.0,
        center_dec_deg=22.0,
        n_stars=10,
        rms_arcsec=0.4,
        matches=(),
        rows=(),
    )


def install_mock_solver(monkeypatch, fail_indices: frozenset[int] = frozenset()):
    """Monkeypatch ``astrometry.session.solve_frame``; records
    ``(index, solver, kwargs, thread_id)`` per call; raises
    :class:`SolveError` for the frames whose index is in ``fail_indices``."""
    calls: list[tuple[int, str, dict, int]] = []

    def fake_solve_frame(*, data, header, solver="auto", **kwargs):
        index = sub_index(header)
        calls.append((index, solver, dict(kwargs), threading.get_ident()))
        if index in fail_indices:
            raise SolveError(f"mock: frame {index} has only 0 sources detected")
        return fake_solve_result(index)

    monkeypatch.setattr(astrometry_session, "solve_frame", fake_solve_frame)
    return calls


class QueuedInvoke:
    """An ``invoke_on_main`` that queues callbacks instead of running them —
    the test pumps the queue from the "main" thread (mirrors ``GLib.idle_add``,
    which is fire-and-forget: :meth:`RunController._call_store` adds its own
    Event wait around the queued callable). Records the thread that RAN each
    queued call."""

    def __init__(self) -> None:
        self.items: queue.Queue = queue.Queue()
        self.ran_on: list[int] = []

    def __call__(self, callback) -> int:
        self.items.put(callback)
        return 1

    def pump_one(self, timeout: float = 0.05) -> bool:
        try:
            callback = self.items.get(timeout=timeout)
        except queue.Empty:
            return False
        self.ran_on.append(threading.get_ident())
        callback()
        return True

    def drain(self) -> None:
        while self.pump_one(timeout=0.0):
            pass


def pump_while_alive(queued: QueuedInvoke, worker: threading.Thread) -> None:
    """The test's "main loop": pump queued store calls until the worker ends."""
    deadline = time.monotonic() + 60.0
    while worker.is_alive() and time.monotonic() < deadline:
        queued.pump_one(timeout=0.05)
    worker.join(timeout=10.0)
    assert not worker.is_alive()
    queued.drain()


# --- solve_session_unsolved: happy path -----------------------------------------------------


def test_solve_session_unsolved_happy_path(project, monkeypatch):
    sid, frames = make_session(project, n=4)
    calls = install_mock_solver(monkeypatch)
    ctrl = RunController(project=project)
    assert ctrl.solving is False
    assert ctrl.solve_report is None and ctrl.solve_error is None
    progress: list[tuple[int, int, int]] = []

    report = ctrl.solve_session_unsolved(
        sid, on_progress=lambda done, total, frame: progress.append((done, total, frame.id))
    )

    # the report
    assert report.session_id == sid
    assert report.solved_ids == tuple(f.id for f in frames)
    assert report.skipped == ()
    assert report.attempted == 4
    assert report.summary == "4 of 4 unsolved subs solved"
    # the solver was called once per sub, default solver, on the calling thread
    assert [c[0] for c in calls] == [0, 1, 2, 3]
    assert all(c[1] == "auto" for c in calls)
    assert all(c[3] == threading.get_ident() for c in calls)
    # the progress sequence is 1..N over the frames, on the calling thread
    assert [p[0] for p in progress] == [1, 2, 3, 4]
    assert all(p[1] == 4 for p in progress)
    assert [p[2] for p in progress] == [f.id for f in frames]
    # every sub got its frame_wcs record (and the WCS cards in its FITS header)
    assert tuple(w.frame_id for w in list_solved_frames(project, sid)) == tuple(
        f.id for f in frames
    )
    header = pyfits.getheader(str(astrometry_session.get_frame_path(project, frames[0])))
    assert header["CRVAL1"] == 83.0
    # the controller state
    assert ctrl.solve_report is report
    assert ctrl.solve_error is None
    assert ctrl.solving is False
    # the WCS coverage now reports full
    cov = ctrl.wcs_coverage(sid)
    assert cov.summary == "4 of 4 subs solved"
    assert cov.fraction == pytest.approx(1.0)


def test_solve_session_unsolved_per_frame_failure_is_skipped_and_kwargs_forwarded(project, monkeypatch):
    sid, frames = make_session(project, n=4)
    calls = install_mock_solver(monkeypatch, fail_indices=frozenset({2}))
    ctrl = RunController(project=project)

    report = ctrl.solve_session_unsolved(
        sid, solver="gaia", pixel_scale_hint=3.6, gaia_query=lambda *a: []
    )

    assert all(c[1] == "gaia" and c[2].get("pixel_scale_hint") == 3.6 for c in calls)
    assert all(callable(c[2].get("gaia_query")) for c in calls)
    assert report.solved_ids == (frames[0].id, frames[1].id, frames[3].id)
    assert report.skipped_ids == (frames[2].id,)
    assert "only 0 sources" in report.skipped[0].reason
    assert report.summary == "3 of 4 unsolved subs solved (1 skipped)"
    # db: written for the solved, nothing for the skipped
    assert {w.frame_id for w in list_solved_frames(project, sid)} == {
        frames[0].id,
        frames[1].id,
        frames[3].id,
    }
    assert ctrl.wcs_coverage(sid).summary == "3 of 4 subs solved"
    # a second batch retries only the still-unsolved sub (it has no frame_wcs
    # row) and skips it again — solved frames are never re-attempted
    calls.clear()
    report2 = ctrl.solve_session_unsolved(sid)
    assert [c[0] for c in calls] == [2]
    assert report2.solved == () and report2.skipped_ids == (frames[2].id,)
    assert report2.summary == "0 of 1 unsolved subs solved (1 skipped)"


def test_solve_session_unsolved_only_unsolved_frames_are_attempted(project, monkeypatch):
    sid, frames = make_session(project, n=4)
    save_frame_wcs(
        project, frames[1].id, solver="solve-field", n_stars=8, center_ra_deg=83.0,
        center_dec_deg=22.0, cards=dict(CARDS), rms_arcsec=0.7,
    )
    calls = install_mock_solver(monkeypatch)
    ctrl = RunController(project=project)

    report = ctrl.solve_session_unsolved(sid)

    assert [c[0] for c in calls] == [0, 2, 3]  # frame 1 already solved: untouched
    assert report.solved_ids == (frames[0].id, frames[2].id, frames[3].id)
    assert ctrl.wcs_coverage(sid).summary == "4 of 4 subs solved"
    # and a fully-solved session is an empty batch
    report2 = ctrl.solve_session_unsolved(sid)
    assert report2.attempted == 0
    assert report2.summary == f"no unsolved subs in session {sid!r}"


# --- solve_session_unsolved: error paths ------------------------------------------------------


def test_solve_session_unsolved_requires_a_project():
    ctrl = RunController()  # no project set
    with pytest.raises(RunError, match="no project"):
        ctrl.solve_session_unsolved("any-session")


def test_solve_session_unsolved_validates_session_id(project):
    ctrl = RunController(project=project)
    with pytest.raises(RunError, match="session_id"):
        ctrl.solve_session_unsolved(42)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(RunError, match="session_id"):
        ctrl.solve_session_unsolved("")
    with pytest.raises(RunError, match="on_progress"):
        ctrl.solve_session_unsolved("x", on_progress="nope")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_solve_session_unsolved_unknown_session_names_the_known_ones(project, monkeypatch):
    sid, _frames = make_session(project, n=2)
    install_mock_solver(monkeypatch)
    ctrl = RunController(project=project)
    with pytest.raises(RunError, match=f"no session 'no-such'.*{re.escape(sid)}"):
        ctrl.solve_session_unsolved("no-such")
    assert ctrl.solve_error is not None and "no-such" in ctrl.solve_error
    assert ctrl.solving is False


# --- solve_session_unsolved: busy guard (one run-or-solve at a time) ---------------------------


def test_solve_session_unsolved_busy_guard(project, monkeypatch):
    sid, _frames = make_session(project, n=2)
    entered = threading.Event()
    release = threading.Event()

    def blocking_solve(*, data, header, solver="auto", **kwargs):
        entered.set()
        assert release.wait(30.0)
        return fake_solve_result(sub_index(header))

    monkeypatch.setattr(astrometry_session, "solve_frame", blocking_solve)
    queued = QueuedInvoke()
    ctrl = RunController(project=project, invoke_on_main=queued)
    result: dict = {}

    def work():
        result["report"] = ctrl.solve_session_unsolved(sid)

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    # pump the marshalled store calls while waiting for the batch to reach the
    # (blocked) solver — the worker cannot get past the session lookup until
    # the main thread pumps it
    deadline = time.monotonic() + 30.0
    while not entered.is_set() and time.monotonic() < deadline:
        queued.pump_one(timeout=0.05)
    assert entered.is_set()  # the batch is in progress (blocked in the solver)
    # a concurrent call is rejected immediately, while a run would be too
    with pytest.raises(RunError, match="already in progress"):
        ctrl.solve_session_unsolved(sid)
    release.set()
    pump_while_alive(queued, worker)

    assert result["report"].attempted == 2
    assert ctrl.solving is False
    assert ctrl.solve_report is result["report"]


# --- worker-thread safety: store calls marshalled, solve on the worker --------------------------


def test_solve_session_unsolved_from_a_worker_thread_marshals_store_calls(project, monkeypatch):
    sid, _frames = make_session(project, n=4)
    calls = install_mock_solver(monkeypatch, fail_indices=frozenset({1}))
    queued = QueuedInvoke()
    ctrl = RunController(project=project, invoke_on_main=queued)
    progress_threads: list[int] = []
    result: dict = {}

    def work():
        result["report"] = ctrl.solve_session_unsolved(
            sid, on_progress=lambda done, total, frame: progress_threads.append(
                threading.get_ident()
            )
        )

    worker = threading.Thread(target=work, name="test-photometry-solve", daemon=True)
    worker.start()
    main_id = threading.get_ident()
    pump_while_alive(queued, worker)

    report = result["report"]
    assert len(report.solved) == 3 and len(report.skipped) == 1
    # every store call ran on the DB-OWNING (main) thread via invoke_on_main...
    assert queued.ran_on and all(t == main_id for t in queued.ran_on)
    assert worker.ident != main_id
    # ...while the slow solves + the progress callbacks stayed on the worker
    assert calls and all(c[3] == worker.ident for c in calls)
    assert progress_threads == [worker.ident] * 4
    # and the results really landed in the db
    assert len(list_solved_frames(project, sid)) == 3
    assert ctrl.solve_report is report and ctrl.solving is False


# --- wcs_coverage ------------------------------------------------------------------------------


def test_wcs_coverage_counts_subs_only(project):
    sid, frames = make_session(project, n=4)
    save_frame_wcs(
        project, frames[0].id, solver="gaia", n_stars=10, center_ra_deg=83.0,
        center_dec_deg=22.0, cards=dict(CARDS), rms_arcsec=0.4,
    )
    stack = store_frame(project, sid, KIND_STACK, sub_fits(99))
    save_frame_wcs(  # a solved STACK must not count towards the sub coverage
        project, stack.id, solver="gaia", n_stars=10, center_ra_deg=83.0,
        center_dec_deg=22.0, cards=dict(CARDS), rms_arcsec=0.4,
    )
    ctrl = RunController(project=project)

    cov = ctrl.wcs_coverage(sid)

    assert cov.session_id == sid
    assert cov.solved == 1 and cov.total == 4 and cov.unsolved == 3
    assert cov.summary == "1 of 4 subs solved"
    assert cov.fraction == pytest.approx(0.25)


def test_wcs_coverage_empty_session_and_error_paths(project):
    sid = start_session(project, clock=frozen(T0))  # no subs
    ctrl = RunController(project=project)
    assert ctrl.wcs_coverage(sid).summary == "no subs in the session"
    with pytest.raises(RunError, match="no session"):
        ctrl.wcs_coverage("no-such-session")
    with pytest.raises(RunError, match="session_id"):
        ctrl.wcs_coverage(42)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(RunError, match="no project"):
        RunController().wcs_coverage(sid)


# --- package boundary (subprocess + source scan) -------------------------------------------------


def test_run_module_imports_no_gi_no_astropy_and_widget_stays_lazy():
    code = (
        "import sys;"
        "import seestar_controller.photometry;"
        "import seestar_controller.photometry.run;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'astropy' not in sys.modules, 'astropy imported eagerly';"
        "assert 'seestar_controller.link' not in sys.modules;"
        "assert 'seestar_controller.ui' not in sys.modules;"
        "assert 'seestar_controller.enhance' not in sys.modules;"
        "assert 'seestar_controller.photometry.results_view' not in sys.modules;"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_run_module_source_scan_no_gi_no_widget_import():
    source = (PHOTOMETRY_PACKAGE_DIR / "run.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE)
    assert not re.search(
        r"^\s*(from|import)\s+.*(results_view|star_picker|suggestions_panel)", source, re.MULTILINE
    )
    assert not re.search(r"^\s*(from|import)\s+.*\b(ui|link|enhance)\b", source, re.MULTILINE)
