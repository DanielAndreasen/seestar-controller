"""Issue 10: the pure batch plate-solve of a session's unsolved subs — tests
for :func:`seestar_controller.astrometry.session.solve_session_unsolved`,
:func:`~seestar_controller.astrometry.session.session_wcs_coverage` and the
``call_store`` split of
:func:`~seestar_controller.astrometry.session.solve_project_frame`.

The solver itself is MOCKED (``seestar_controller.astrometry.session.solve_frame``
is monkeypatched) — no solve-field subprocess, no Gaia network; the frames are
real stored FITS files, so the prepare/persist halves (FITS read, header
rewrite, ``frame_wcs`` upsert) run for real. Covered: only the unsolved frames
are attempted (``list_solved_frames``), per-frame :class:`SolveError` tolerated
as skipped (never kills the batch), the progress sequence ``1..N``, the stride,
the frozen report contents, the db-thread marshalling of every store call while
the solve stays on the calling worker thread, and the WCS coverage helper.
"""

from __future__ import annotations

import queue
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest
from astropy.io import fits as pyfits

from seestar_controller.astrometry import (
    SessionSolveReport,
    SessionWcsCoverage,
    SkippedSolve,
    SolvedFrame,
    SolveError,
    session_wcs_coverage,
    solve_project_frame,
    solve_session_unsolved,
)
from seestar_controller.astrometry import session as astrometry_session
from seestar_controller.astrometry.solve import SolveResult
from seestar_controller.store import (
    KIND_STACK,
    KIND_SUB,
    create_project,
    save_frame_wcs,
    start_session,
    store_frame,
)
from seestar_controller.store.errors import SessionNotFoundError
from seestar_controller.store.wcs import list_solved_frames, load_frame_wcs

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)

#: A valid TAN WCS card set (same shape as tests/test_astrometry_store.py).
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
    p = create_project("M 42", root=root)
    yield p
    p.close()


def frozen(dt: datetime):
    return lambda: dt


def sub_fits(i: int) -> bytes:
    """A small valid FITS sub with a per-index DATE-OBS (T0 + i*10s)."""
    rng = np.random.default_rng(100 + i)
    data = np.ascontiguousarray(
        np.full((16, 16), 100.0) + rng.normal(0.0, 2.0, (16, 16)), dtype=np.float32
    )
    hdu = pyfits.PrimaryHDU(data=data)
    hdu.header["DATE-OBS"] = (T0 + timedelta(seconds=10 * i)).isoformat(timespec="milliseconds")
    import io

    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def sub_index(header) -> int:
    """The sub index encoded in the DATE-OBS card (seconds offset / 10)."""
    return (int(datetime.fromisoformat(header["DATE-OBS"]).timestamp() - T0.timestamp()) // 10)


def add_subs(project, session_id: str, n: int) -> list:
    """Store ``n`` subs (index 0..n-1) in the session; returns the Frames in
    jd order."""
    return [
        store_frame(project, session_id, KIND_SUB, sub_fits(i)) for i in range(n)
    ]


def fake_solve_result(index: int) -> SolveResult:
    return SolveResult(
        wcs=None,  # the persist side only uses cards/center/... — never wcs
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
    """Monkeypatch ``astrometry.session.solve_frame`` with a fake that records
    ``(index, solver, kwargs, thread_id)`` per call and returns a valid
    :class:`SolveResult` — unless the frame's DATE-OBS index is in
    ``fail_indices``, then raises :class:`SolveError` (a per-frame failure)."""
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
    """A blocking :data:`StoreCall`: queues the callable and WAITS for the
    "main" thread (the test pump, mirroring ``GLib.idle_add`` in the app) to
    run it, then returns its result — exactly the
    ``RunController._call_store`` marshalling (``invoke_on_main`` + Event
    wait). Records the thread that RAN each call."""

    def __init__(self) -> None:
        self.items: queue.Queue = queue.Queue()
        self.ran_on: list[int] = []

    def __call__(self, callback) -> Any:
        box: dict[str, Any] = {}
        done = threading.Event()

        def wrapper() -> None:
            try:
                box["result"] = callback()
            except BaseException as exc:  # re-raised on the calling thread  # noqa: BLE001
                box["error"] = exc
            finally:
                done.set()

        self.items.put(wrapper)
        if not done.wait(60.0):
            raise RuntimeError("the pump did not run the queued store call")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def pump_one(self, timeout: float = 0.05) -> bool:
        try:
            wrapper = self.items.get(timeout=timeout)
        except queue.Empty:
            return False
        self.ran_on.append(threading.get_ident())
        wrapper()
        return True

    def drain(self) -> None:
        while self.pump_one(timeout=0.0):
            pass


# --- solve_session_unsolved: only the unsolved frames are attempted ---------------------------


def test_solve_session_unsolved_attempts_only_unsolved_and_persists(project, monkeypatch):
    sid = start_session(project, clock=frozen(T0))
    frames = add_subs(project, sid, 5)
    # frame index 1 is ALREADY solved (a stored frame_wcs row)
    save_frame_wcs(
        project,
        frames[1].id,
        solver="solve-field",
        n_stars=8,
        center_ra_deg=83.0,
        center_dec_deg=22.0,
        cards=dict(CARDS),
        rms_arcsec=0.7,
        solved_at="2026-11-21T00:00:00.000+00:00",
    )
    before = load_frame_wcs(project, frames[1].id)
    calls = install_mock_solver(monkeypatch)
    progress: list[tuple[int, int, int]] = []

    report = solve_session_unsolved(
        project, sid, lambda done, total, frame: progress.append((done, total, frame.id))
    )

    # only the 4 unsolved subs were attempted, in frame (jd) order
    assert [c[0] for c in calls] == [0, 2, 3, 4]
    assert all(c[1] == "auto" for c in calls)  # the default solver forwarded
    assert report.session_id == sid
    assert report.solved_ids == tuple(f.id for i, f in enumerate(frames) if i != 1)
    assert report.skipped == ()
    assert report.attempted == 4
    assert report.summary == "4 of 4 unsolved subs solved"
    assert all(isinstance(s, SolvedFrame) and s.result.solver == "gaia" for s in report.solved)
    # the progress sequence is 1..N over the attempted frames
    assert [p[0] for p in progress] == [1, 2, 3, 4]
    assert all(p[1] == 4 for p in progress)
    assert [p[2] for p in progress] == [f.id for i, f in enumerate(frames) if i != 1]
    # every solved frame got its db record + the WCS in the stored FITS header
    for i, frame in enumerate(frames):
        if i == 1:
            continue
        record = load_frame_wcs(project, frame.id)
        assert record is not None and record.solver == "gaia" and record.n_stars == 10
        header = pyfits.getheader(str(astrometry_session.get_frame_path(project, frame)))
        assert header["CRVAL1"] == 83.0 and header["CTYPE1"] == "RA---TAN"
    # the already-solved frame was NOT touched
    after = load_frame_wcs(project, frames[1].id)
    assert after.solver == "solve-field" and after.n_stars == 8  # ty: ignore[missing-union-attribute]
    assert after.solved_at == before.solved_at  # ty: ignore[missing-union-attribute]
    assert len(list_solved_frames(project, sid)) == 5


def test_solve_session_unsolved_tolerates_per_frame_solve_errors(project, monkeypatch):
    """A per-frame :class:`SolveError` is recorded as skipped and NEVER kills
    the batch; nothing is written for a skipped frame."""
    sid = start_session(project, clock=frozen(T0))
    frames = add_subs(project, sid, 5)
    calls = install_mock_solver(monkeypatch, fail_indices=frozenset({1, 3}))
    progress: list[int] = []

    report = solve_session_unsolved(
        project, sid, lambda done, total, frame: progress.append(done)
    )

    # all 5 unsolved subs were attempted (the failures included), 1..N
    assert [c[0] for c in calls] == [0, 1, 2, 3, 4]
    assert progress == [1, 2, 3, 4, 5]
    assert report.solved_ids == (frames[0].id, frames[2].id, frames[4].id)
    assert report.skipped_ids == (frames[1].id, frames[3].id)
    assert all(isinstance(s, SkippedSolve) for s in report.skipped)
    assert all("only 0 sources" in s.reason for s in report.skipped)
    assert report.summary == "3 of 5 unsolved subs solved (2 skipped)"
    # db + header: written for the solved, untouched for the skipped
    for i, frame in enumerate(frames):
        record = load_frame_wcs(project, frame.id)
        header = pyfits.getheader(str(astrometry_session.get_frame_path(project, frame)))
        if i in (1, 3):
            assert record is None
            assert "CRVAL1" not in header
        else:
            assert record is not None and record.solver == "gaia"
            assert header["CRVAL1"] == 83.0


def test_solve_session_unsolved_stride_solves_every_nth(project, monkeypatch):
    sid = start_session(project, clock=frozen(T0))
    frames = add_subs(project, sid, 6)
    calls = install_mock_solver(monkeypatch)

    report = solve_session_unsolved(project, sid, stride=2)

    assert [c[0] for c in calls] == [0, 2, 4]  # every 2nd unsolved sub
    assert report.solved_ids == (frames[0].id, frames[2].id, frames[4].id)
    assert report.summary == "3 of 3 unsolved subs solved"
    # the default stride=1 solves ALL
    calls.clear()
    report2 = solve_session_unsolved(project, sid)  # only 1, 3, 5 remain unsolved
    assert [c[0] for c in calls] == [1, 3, 5]
    assert report2.attempted == 3


def test_solve_session_unsolved_forwards_solver_kwargs(project, monkeypatch):
    sid = start_session(project, clock=frozen(T0))
    add_subs(project, sid, 2)
    calls = install_mock_solver(monkeypatch)

    report = solve_session_unsolved(
        project, sid, solver="gaia", pixel_scale_hint=3.6, gaia_query=lambda *a: []
    )

    assert all(c[1] == "gaia" for c in calls)
    assert all(c[2].get("pixel_scale_hint") == 3.6 for c in calls)
    assert all(callable(c[2].get("gaia_query")) for c in calls)
    assert report.attempted == 2


def test_solve_session_unsolved_validation_and_unknown_session(project, monkeypatch):
    sid = start_session(project, clock=frozen(T0))
    add_subs(project, sid, 2)
    install_mock_solver(monkeypatch)
    with pytest.raises(TypeError, match="session_id"):
        solve_session_unsolved(project, 42)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="session_id"):
        solve_session_unsolved(project, "")
    with pytest.raises(TypeError, match="on_progress"):
        solve_session_unsolved(project, sid, on_progress="not-callable")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="stride"):
        solve_session_unsolved(project, sid, stride="2")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="stride"):
        solve_session_unsolved(project, sid, stride=0)
    with pytest.raises(SessionNotFoundError, match="no session"):
        solve_session_unsolved(project, "no-such-session")


def test_solve_session_unsolved_empty_session_is_a_valid_empty_batch(project, monkeypatch):
    sid = start_session(project, clock=frozen(T0))  # no subs at all
    calls = install_mock_solver(monkeypatch)
    progress: list = []

    report = solve_session_unsolved(
        project, sid, lambda done, total, frame: progress.append(done)
    )

    assert calls == []
    assert progress == []
    assert report.solved == () and report.skipped == ()
    assert report.attempted == 0
    assert report.summary == f"no unsolved subs in session {sid!r}"


def test_solve_session_unsolved_report_is_frozen(project, monkeypatch):
    sid = start_session(project, clock=frozen(T0))
    add_subs(project, sid, 1)
    install_mock_solver(monkeypatch)
    report = solve_session_unsolved(project, sid)
    assert isinstance(report, SessionSolveReport)
    with pytest.raises(Exception):  # noqa: B017 — dataclasses.FrozenInstanceError
        report.session_id = "other"  # type: ignore[misc]  # ty: ignore[invalid-assignment]
    with pytest.raises(Exception):  # noqa: B017
        report.solved = ()  # type: ignore[misc]  # ty: ignore[invalid-assignment]


# --- solve_session_unsolved: db-thread marshalling (call_store) ---------------------------------


def test_solve_session_unsolved_from_a_worker_thread_marshals_store_calls(project, monkeypatch):
    """The batch runs on a WORKER thread while every store read/write goes
    through ``call_store`` (pumped by the db-owning main thread here, like
    ``GLib.idle_add`` in the app) — and the slow solve itself stays on the
    worker thread."""
    sid = start_session(project, clock=frozen(T0))
    add_subs(project, sid, 4)
    calls = install_mock_solver(monkeypatch, fail_indices=frozenset({2}))
    queued = QueuedInvoke()
    result: dict = {}
    progress_threads: list[int] = []

    def work():
        result["report"] = solve_session_unsolved(
            project,
            sid,
            lambda done, total, frame: progress_threads.append(threading.get_ident()),
            call_store=queued,
        )

    worker = threading.Thread(target=work, name="test-batch-solve", daemon=True)
    worker.start()
    main_id = threading.get_ident()
    import time

    deadline = time.monotonic() + 60.0
    while worker.is_alive() and time.monotonic() < deadline:
        queued.pump_one(timeout=0.05)  # the "main loop" runs the store calls
    worker.join(timeout=10.0)
    assert not worker.is_alive()
    queued.drain()

    report = result["report"]
    assert report.solved_ids and len(report.solved) == 3 and len(report.skipped) == 1
    # every marshalled store call ran on the DB-OWNING (main) thread...
    assert queued.ran_on and all(t == main_id for t in queued.ran_on)
    # ...while the solver + progress ran on the WORKER thread
    assert calls and all(c[3] == worker.ident for c in calls)
    assert progress_threads and all(t == worker.ident for t in progress_threads)
    assert worker.ident != main_id
    # the store calls really happened: listing (3) + prepare + persist per
    # SOLVED frame, but only prepare for a skipped frame (no persist on failure)
    assert len(queued.ran_on) == 3 + 2 * len(report.solved) + len(report.skipped)
    # and the results landed in the db (read from the main thread)
    assert len(list_solved_frames(project, sid)) == 3


def test_solve_project_frame_call_store_splits_db_parts_from_the_solve(project, monkeypatch):
    """``solve_project_frame(..., call_store=...)`` runs ONLY the db-touching
    halves (prepare: frame resolve + FITS read; persist: header rewrite + db
    upsert) through the marshaller — the solve stays on the caller."""
    sid = start_session(project, clock=frozen(T0))
    frames = add_subs(project, sid, 1)
    calls = install_mock_solver(monkeypatch)
    queued = QueuedInvoke()
    result: dict = {}

    def work():
        result["r"] = solve_project_frame(
            project, frames[0], call_store=queued, solver="gaia", pixel_scale_hint=3.6
        )

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    import time

    main_id = threading.get_ident()
    deadline = time.monotonic() + 60.0
    while worker.is_alive() and time.monotonic() < deadline:
        queued.pump_one(timeout=0.05)
    worker.join(timeout=10.0)
    assert not worker.is_alive()

    assert result["r"].solver == "gaia"
    assert len(queued.ran_on) == 2  # exactly prepare + persist
    assert all(t == main_id for t in queued.ran_on)
    assert calls and calls[0][3] == worker.ident  # the solve on the worker
    assert calls[0][1] == "gaia" and calls[0][2].get("pixel_scale_hint") == 3.6
    assert load_frame_wcs(project, frames[0].id) is not None


def test_solve_project_frame_default_call_store_is_unchanged(project, monkeypatch):
    """Without ``call_store`` everything runs directly on the calling thread
    (the pre-issue-10 behavior)."""
    sid = start_session(project, clock=frozen(T0))
    frames = add_subs(project, sid, 1)
    calls = install_mock_solver(monkeypatch)
    main_id = threading.get_ident()

    result = solve_project_frame(project, frames[0], solver="gaia")

    assert result.solver == "gaia"
    assert calls[0][3] == main_id  # direct: same thread
    assert load_frame_wcs(project, frames[0].id) is not None


# --- session_wcs_coverage ------------------------------------------------------------------------


def test_session_wcs_coverage_counts_subs_only(project):
    sid = start_session(project, clock=frozen(T0))
    frames = add_subs(project, sid, 4)
    # one solved sub + one solved STACK of the same session (stacks don't count)
    save_frame_wcs(
        project, frames[2].id, solver="gaia", n_stars=10, center_ra_deg=83.0,
        center_dec_deg=22.0, cards=dict(CARDS), rms_arcsec=0.4,
    )
    stack = store_frame(
        project, sid, KIND_STACK, sub_fits(99)
    )
    save_frame_wcs(
        project, stack.id, solver="gaia", n_stars=10, center_ra_deg=83.0,
        center_dec_deg=22.0, cards=dict(CARDS), rms_arcsec=0.4,
    )

    cov = session_wcs_coverage(project, sid)

    assert isinstance(cov, SessionWcsCoverage)
    assert cov.session_id == sid
    assert cov.solved == 1 and cov.total == 4
    assert cov.unsolved == 3
    assert cov.fraction == pytest.approx(0.25)
    assert cov.summary == "1 of 4 subs solved"
    with pytest.raises(Exception):  # noqa: B017 — frozen
        cov.solved = 0  # type: ignore[misc]  # ty: ignore[invalid-assignment]


def test_session_wcs_coverage_empty_session_and_unknown_session(project):
    sid = start_session(project, clock=frozen(T0))
    cov = session_wcs_coverage(project, sid)
    assert cov.solved == 0 and cov.total == 0 and cov.fraction == 0.0
    assert cov.summary == "no subs in the session"
    with pytest.raises(SessionNotFoundError, match="no session"):
        session_wcs_coverage(project, "no-such-session")
    with pytest.raises(TypeError, match="session_id"):
        session_wcs_coverage(project, 42)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_session_wcs_coverage_uses_call_store(project):
    sid = start_session(project, clock=frozen(T0))
    frames = add_subs(project, sid, 3)
    save_frame_wcs(
        project, frames[0].id, solver="gaia", n_stars=10, center_ra_deg=83.0,
        center_dec_deg=22.0, cards=dict(CARDS), rms_arcsec=0.4,
    )
    seen: list[int] = []

    def recording_call(fn):
        seen.append(threading.get_ident())
        return fn()

    cov = session_wcs_coverage(project, sid, call_store=recording_call)
    assert cov.summary == "1 of 3 subs solved"
    assert len(seen) == 3  # session check + frames + solved frames
    assert all(t == threading.get_ident() for t in seen)


def test_solve_then_coverage_reflects_the_batch(project, monkeypatch):
    """The issue-10 flow: batch-solve the unsolved subs, then the coverage is
    full (this is what the Run photometry tab shows near the Run button)."""
    sid = start_session(project, clock=frozen(T0))
    frames = add_subs(project, sid, 5)
    save_frame_wcs(
        project, frames[0].id, solver="solve-field", n_stars=8, center_ra_deg=83.0,
        center_dec_deg=22.0, cards=dict(CARDS), rms_arcsec=0.7,
    )
    install_mock_solver(monkeypatch, fail_indices=frozenset({3}))
    assert session_wcs_coverage(project, sid).summary == "1 of 5 subs solved"

    report = solve_session_unsolved(project, sid)
    cov = session_wcs_coverage(project, sid)

    assert report.summary == "3 of 4 unsolved subs solved (1 skipped)"
    assert cov.summary == "4 of 5 subs solved"  # 1 pre-solved + 3 new; 1 failed
    assert cov.unsolved == 1
