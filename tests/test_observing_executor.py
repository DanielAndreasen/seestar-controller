"""Observing ticket 02 — the night executor state machine (headless).

:class:`~seestar_controller.observing.executor.NightExecutor` drives the link
Device facade through the plan queue (idle -> resolving -> slewing ->
stacking -> next | done), auto-advancing, opening/closing the project session.

Two device doubles, per docs/wiki/assumptions.md (no real scope, user AFK —
all link work is mock-only):

- the **real Device facade against the mock Scope** on 127.0.0.1 (conftest
  ``transport`` / ``mock_scope`` fixtures) for the end-to-end runs — both
  targets visited, session opened/closed, park at end, scope errors mid-stack;
- a **FakeDevice** (same facade shape, scripted errors, manual progress/event
  emission) for the deterministic command tests (pause/resume/skip/stop,
  frames budget, invoke_on_main marshalling, call order).

Plus a subprocess test enforcing the no-``gi`` boundary of the executor module
(the package itself stays link-free until the executor names are touched).
"""

import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from seestar_controller import observing
from seestar_controller.catalog.coords import J2000Coord
from seestar_controller.link.device import Device, StackingProgress, plain_rpc_message
from seestar_controller.link.protocol import Reply
from seestar_controller.link.transport import RpcError
from seestar_controller.observing import (
    ERROR_POLICIES,
    ERROR_POLICY_ABORT,
    ERROR_POLICY_SKIP,
    EXECUTOR_STATES,
    ExecutorEvent,
    ExecutorSnapshot,
    ExecutorState,
    ExecutorStateError,
    NightExecutor,
)
from seestar_controller.observing.queue import PlanQueue, QueueEntry
from seestar_controller.store.project import create_project
from seestar_controller.store.sessions import (
    close_session,
    get_open_session,
    list_sessions,
    start_session,
)

GOTO_EVENT = "ScopeGoto"


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def coord_entry(ra_hours: float, dec_deg: float, **kwargs) -> QueueEntry:
    return QueueEntry(coord=J2000Coord.from_hours(ra_hours, dec_deg), **kwargs)


def two_target_queue() -> PlanQueue:
    return PlanQueue([coord_entry(5.5, -5.0), coord_entry(10.0, 20.0)])


def rpc_error(method: str, code: int, result: str = "error", req_id: int = 1) -> RpcError:
    """An RpcError as the transport raises it for a non-zero reply ``code``."""
    return RpcError(Reply(id=req_id, method=method, code=code, result=result))


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    p = create_project("Test Night", root=root)
    yield p
    p.close()


@pytest.fixture()
def device(transport, mock_scope):
    """A real Device facade on the connected transport fixture, fast mock."""
    mock_scope.slew_duration = 0.05
    mock_scope.stack_event_interval = 0.02
    d = Device.connect("127.0.0.1", transport=transport, goto_timeout=2.0)
    yield d
    d.disconnect()


def goto_completes(device) -> list:
    """Collect the ``ScopeGoto`` "complete" payloads (one per visited target)."""
    gotos: list = []

    def on_goto(event):
        if event.payload.get("state") == "complete":
            gotos.append(event.payload)

    device.add_event_handler(GOTO_EVENT, on_goto)
    return gotos


class FakeDevice:
    """Duck-typed stand-in for the link Device facade — no network, no gi.

    Records every call in ``calls``; scripted errors (``goto_error`` /
    ``stack_errors``) raise on the matching call; :meth:`emit_progress` and
    :meth:`emit_event` drive the executor's handlers like the transport does.
    """

    def __init__(self):
        self.calls: list[tuple] = []
        self._handlers: dict[str, list] = {}
        self._progress_handlers: list = []
        self.goto_error: Exception | None = None
        self.stack_errors: list[Exception] = []
        self.park_errors: list[Exception] = []
        #: the mode ``state()`` reports (issue 11: "stacking" simulates a
        #: scope left stacking by a run that died mid-stack)
        self.mode = "idle"
        self._lock = threading.Lock()

    def goto(self, ra, dec, *, name=None, timeout=None):
        with self._lock:
            self.calls.append(("goto", ra, dec, name))
            error = self.goto_error
        if error is not None:
            raise error
        return SimpleNamespace(mode="idle", target=(ra, dec))

    def park(self):
        with self._lock:
            self.calls.append(("park",))
            error = self.park_errors.pop(0) if self.park_errors else None
        if error is not None:
            raise error
        with self._lock:
            self.mode = "parked"
        return SimpleNamespace(mode="parked")

    def start_stack(self, exposure, gain, mosaic=False, *, timeout=None):
        with self._lock:
            self.calls.append(("start_stack", exposure, gain, mosaic))
            error = self.stack_errors.pop(0) if self.stack_errors else None
        if error is not None:
            raise error
        with self._lock:
            self.mode = "stacking"
        return SimpleNamespace(mode="stacking")

    def stop_stack(self, *, timeout=None):
        with self._lock:
            self.calls.append(("stop_stack",))
            self.mode = "idle"
        return SimpleNamespace(mode="idle")

    def state(self):
        with self._lock:
            mode = self.mode
        return SimpleNamespace(
            mode=mode,
            stacking=SimpleNamespace(
                state="working" if mode == "stacking" else "idle", lapse_ms=0, frames=0
            ),
        )

    def add_event_handler(self, name, handler):
        self._handlers.setdefault(name, []).append(handler)

    def remove_event_handler(self, name, handler):
        handlers = self._handlers.get(name, [])
        if handler in handlers:
            handlers.remove(handler)

    def add_stacking_progress_handler(self, handler):
        self._progress_handlers.append(handler)

    def remove_stacking_progress_handler(self, handler):
        if handler in self._progress_handlers:
            self._progress_handlers.remove(handler)

    def emit_progress(self, frames: int, state: str = "working", lapse_ms: int = 0):
        progress = StackingProgress(state=state, lapse_ms=lapse_ms, frames=frames)
        for handler in list(self._progress_handlers):
            handler(progress)

    def emit_event(self, name: str, payload: dict | None = None):
        event = SimpleNamespace(name=name, payload=payload or {})
        for handler in list(self._handlers.get(name, [])):
            handler(event)

    def calls_of(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]


# --- end-to-end against the real facade + mock Scope ------------------------------


def test_two_target_run_visits_both_targets_reaches_done_and_closes_session(
    device, mock_scope, project
):
    gotos = goto_completes(device)
    ex = NightExecutor(device, two_target_queue(), project, frames_per_target=2)
    snap = ex.run(timeout=15)
    assert snap.state == ExecutorState.DONE
    assert snap.error is None
    # both targets were visited (goto called with each coord)
    assert sorted(g["ra"] for g in gotos) == [5.5, 10.0]
    assert sorted(g["dec"] for g in gotos) == [-5.0, 20.0]
    assert mock_scope.last_goto == {"ra": 10.0, "dec": 20.0}
    # parked at end (park_at_end default True)
    assert device.state().mode == "parked"
    # session opened and closed in the project store
    assert ex.session is not None
    assert get_open_session(project) is None
    sessions = list_sessions(project)
    assert len(sessions) == 1
    assert sessions[0].id == ex.session
    assert sessions[0].ended_at is not None
    # the event log covers session open/close and the park
    details = [e.detail for e in ex.log]
    assert any("opened" in d and ex.session in d for d in details)
    assert any("closed" in d and ex.session in d for d in details)
    assert any("parked" in d for d in details)


def test_listener_receives_snapshot_sequence_with_session(device, project):
    snaps: list[ExecutorSnapshot] = []
    ex = NightExecutor(device, PlanQueue([coord_entry(1.0, 2.0)]), project, frames_per_target=1)
    ex.add_listener(snaps.append)
    snap = ex.run(timeout=15)
    assert snap.state == ExecutorState.DONE
    states = [s.state for s in snaps]
    assert states[0] == ExecutorState.SLEWING
    assert ExecutorState.STACKING in states
    assert states[-1] == ExecutorState.DONE
    assert all(s.session == ex.session for s in snaps)
    stacking = [s for s in snaps if s.state == ExecutorState.STACKING]
    assert stacking[-1].frames_stacked >= 1  # progress snapshots carry the frame count
    assert stacking[-1].queue_index == 0
    assert stacking[-1].entry is not None and stacking[-1].entry.coord is not None


def test_unresolved_name_entry_is_resolved_via_injected_resolver(device, project):
    gotos = goto_completes(device)
    q = PlanQueue([QueueEntry(name="M 42"), coord_entry(7.0, 8.0)])
    resolver_calls: list[str] = []

    def resolver(name: str):
        resolver_calls.append(name)
        return J2000Coord.from_hours(3.25, -15.5)

    snaps: list[ExecutorSnapshot] = []
    ex = NightExecutor(device, q, project, resolver=resolver, frames_per_target=1)
    ex.add_listener(snaps.append)
    snap = ex.run(timeout=15)
    assert snap.state == ExecutorState.DONE
    assert resolver_calls == ["M 42"]
    assert ExecutorState.RESOLVING in [s.state for s in snaps]
    # the entry was replaced in the queue in place (resolved event fired)
    assert q[0].is_resolved
    assert q[0].name == "M 42"
    assert q[0].coord == J2000Coord.from_hours(3.25, -15.5)
    # both targets visited: the resolved one and the coord one
    assert sorted(g["ra"] for g in gotos) == [3.25, 7.0]


def test_goto_passes_the_entry_name_to_the_scope(device, mock_scope, project):
    """Issue 10: the slew carries the queue entry's name as ``name=``, so the
    Scope names the target (and the frames stacked on it) after it — the
    mock records the raw ``scope_goto_target`` wire params."""
    q = PlanQueue([QueueEntry(name="NGC 869")])

    def resolver(name: str):
        return J2000Coord.from_hours(3.5872, 58.2167)

    ex = NightExecutor(device, q, project, resolver=resolver, frames_per_target=1)
    snap = ex.run(timeout=15)
    assert snap.state == ExecutorState.DONE
    # the legacy goto's wire params include the name (mock test hook)
    params = mock_scope.last_wire_params["scope_goto_target"]
    assert params["name"] == "NGC 869"
    assert params["ra"] == pytest.approx(3.5872)
    assert params["dec"] == pytest.approx(58.2167)
    # the mock Scope named the target, and the facade reports the name too
    assert mock_scope.scope_state()["target"]["name"] == "NGC 869"
    assert device.state().target_name == "NGC 869"


def test_goto_falls_back_to_the_ra_dec_string_for_coord_added_entries(project):
    """Issue 10: an entry added by coords has no name — the goto name falls
    back to its RA-Dec string."""
    dev = FakeDevice()
    coord = J2000Coord.from_hours(5.5, -5.0)
    ex = NightExecutor(
        dev, PlanQueue([QueueEntry(coord=coord)]), project, duration_per_target_s=0.05
    )
    snap = ex.run(timeout=5)
    assert snap.state == ExecutorState.DONE
    goto = dev.calls_of("goto")[0]
    assert goto[1:3] == (5.5, -5.0)
    assert goto[3] == str(coord)  # e.g. "RA 5.500000h Dec -5.000000deg"


def test_resolution_failure_skips_entry_under_default_policy(device, project):
    gotos = goto_completes(device)
    q = PlanQueue([QueueEntry(name="NO SUCH OBJECT"), coord_entry(7.0, 8.0)])

    def resolver(name: str):
        raise KeyError(name)

    snaps: list[ExecutorSnapshot] = []
    ex = NightExecutor(device, q, project, resolver=resolver, frames_per_target=1)
    ex.add_listener(snaps.append)
    snap = ex.run(timeout=15)
    assert snap.state == ExecutorState.DONE  # skip policy: the night continues
    assert [g["ra"] for g in gotos] == [7.0]  # only the good target was visited
    assert any("error on entry 0" in e.detail for e in ex.log)
    assert any(s.error is not None and "KeyError" in s.error for s in snaps)


def test_scope_error_mid_stack_skips_to_next_entry(device, project):
    gotos = goto_completes(device)
    ex = NightExecutor(device, two_target_queue(), project, duration_per_target_s=0.5)
    worker = ex.start()
    assert worker is not None
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    # a Scope error reply: raises here AND pushes a scope_error event to the executor
    with pytest.raises(RpcError):
        device.transport.request("mock_fail")
    assert ex.wait(timeout=15)
    snap = ex.snapshot
    assert snap.state == ExecutorState.DONE
    assert sorted(g["ra"] for g in gotos) == [5.5, 10.0]  # both visited despite the error
    assert any("scope_error" in e.detail for e in ex.log)
    assert get_open_session(project) is None
    assert len(list_sessions(project)) == 1


def test_scope_error_with_abort_policy_aborts_run(device, project):
    gotos = goto_completes(device)
    ex = NightExecutor(
        device, two_target_queue(), project, duration_per_target_s=5.0, error_policy="abort"
    )
    assert ex.start() is not None
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    with pytest.raises(RpcError):
        device.transport.request("mock_fail")
    assert ex.wait(timeout=15)
    snap = ex.snapshot
    assert snap.state == ExecutorState.ABORTED
    assert snap.error is not None and "scope_error" in snap.error
    assert [g["ra"] for g in gotos] == [5.5]  # the second target was never slewed
    assert device.state().mode != "parked"  # no park on ABORTED
    assert get_open_session(project) is None  # session still closed
    sessions = list_sessions(project)
    assert len(sessions) == 1 and sessions[0].ended_at is not None


def test_connection_lost_mid_stack_aborts(device, mock_scope, project):
    ex = NightExecutor(
        device, two_target_queue(), project, duration_per_target_s=5.0, error_policy="abort"
    )
    assert ex.start() is not None
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    # wait until the mock is really stacking (start_stack reply in flight done)
    assert wait_until(lambda: mock_scope.scope_state()["mode"] == "stacking")
    mock_scope.drop_connections()  # link loss -> connection_lost event / lost requests
    assert ex.wait(timeout=15)
    snap = ex.snapshot
    assert snap.state == ExecutorState.ABORTED
    assert snap.error is not None and (
        "connection lost" in snap.error or "ConnectionLostError" in snap.error
    )
    assert get_open_session(project) is None


def test_connection_lost_event_mid_stack_aborts_with_event_message(project):
    """The pushed ``connection_lost`` event path (deterministic, FakeDevice)."""
    dev = FakeDevice()
    ex = NightExecutor(
        dev, PlanQueue([coord_entry(1.0, 2.0)]), project, frames_per_target=1000,
        error_policy="abort",
    )
    ex.start()
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    dev.emit_event("connection_lost", {"reason": "socket eof"})
    assert ex.wait(timeout=5)
    snap = ex.snapshot
    assert snap.state == ExecutorState.ABORTED
    assert snap.error is not None and "connection lost: socket eof" in snap.error
    assert dev.calls_of("stop_stack")  # best-effort stop before aborting
    assert not dev.calls_of("park")


def test_duration_per_target_stops_stacking(device, project):
    ex = NightExecutor(device, PlanQueue([coord_entry(2.0, 3.0)]), project, duration_per_target_s=0.3)
    snap = ex.run(timeout=15)
    assert snap.state == ExecutorState.DONE
    assert any("elapsed" in e.detail for e in ex.log)
    assert device.state().mode == "parked"


# --- commands (pause / resume / skip / stop) with the FakeDevice -------------------


def test_pause_resume_during_stacking(project):
    dev = FakeDevice()
    ex = NightExecutor(dev, PlanQueue([coord_entry(5.5, -5.0)]), project, frames_per_target=100)
    ex.start()
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    dev.emit_progress(5)
    assert wait_until(lambda: ex.snapshot.frames_stacked == 5)
    ex.pause()
    assert wait_until(lambda: ex.state == ExecutorState.PAUSED)
    assert dev.calls_of("stop_stack"), "pause stops the current Stack"
    ex.resume()
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    assert len(dev.calls_of("start_stack")) == 2, "resume restarts the Stack"
    dev.emit_progress(6)
    assert wait_until(lambda: ex.snapshot.frames_stacked == 6)
    ex.stop()
    assert ex.wait(timeout=5)
    assert ex.snapshot.state == ExecutorState.DONE
    assert get_open_session(project) is None


def test_skip_advances_to_next_entry(project):
    dev = FakeDevice()
    ex = NightExecutor(dev, two_target_queue(), project, frames_per_target=1000)
    ex.start()
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    assert ex.snapshot.queue_index == 0
    ex.skip()
    assert wait_until(lambda: ex.state == ExecutorState.STACKING and ex.snapshot.queue_index == 1)
    assert dev.calls_of("stop_stack"), "skip stops the current Stack"
    ex.stop()
    assert ex.wait(timeout=5)
    assert ex.snapshot.state == ExecutorState.DONE
    assert len(dev.calls_of("goto")) == 2  # both targets were slewed to


def test_stop_ends_run_in_done_parks_and_closes_session(project):
    dev = FakeDevice()
    ex = NightExecutor(dev, two_target_queue(), project, frames_per_target=1000)
    ex.start()
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    ex.stop()
    assert ex.wait(timeout=5)
    snap = ex.snapshot
    assert snap.state == ExecutorState.DONE
    assert dev.calls_of("park"), "stop() is graceful: DONE + park"
    assert get_open_session(project) is None
    assert len(list_sessions(project)) == 1


def test_park_at_end_false_does_not_park(project):
    dev = FakeDevice()
    ex = NightExecutor(
        dev, PlanQueue([coord_entry(1.0, 2.0)]), project, duration_per_target_s=0.05,
        park_at_end=False,
    )
    snap = ex.run(timeout=5)
    assert snap.state == ExecutorState.DONE
    assert not dev.calls_of("park")


def test_frames_per_target_stops_via_stacking_progress(project):
    dev = FakeDevice()
    ex = NightExecutor(dev, PlanQueue([coord_entry(1.0, 2.0)]), project, frames_per_target=3)
    ex.start()
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    for frames in (1, 2, 3):
        dev.emit_progress(frames)
    assert ex.wait(timeout=5)
    snap = ex.snapshot
    assert snap.state == ExecutorState.DONE
    assert snap.frames_stacked == 3
    assert dev.calls_of("stop_stack"), "the Stack is stopped once the frame budget is hit"


def test_stack_finished_event_ends_target_without_stop(project):
    dev = FakeDevice()
    ex = NightExecutor(dev, PlanQueue([coord_entry(1.0, 2.0)]), project, frames_per_target=1000)
    ex.start()
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    dev.emit_event("stack_finished", {"reason": "scope stopped"})
    assert ex.wait(timeout=5)
    assert ex.snapshot.state == ExecutorState.DONE
    assert not dev.calls_of("stop_stack")
    assert dev.calls_of("park")


def test_facade_call_order_per_entry(project):
    dev = FakeDevice()
    ex = NightExecutor(dev, two_target_queue(), project, duration_per_target_s=0.05)
    snap = ex.run(timeout=5)
    assert snap.state == ExecutorState.DONE
    kinds = [c[0] for c in dev.calls]
    assert kinds == [
        "goto", "start_stack", "stop_stack",
        "goto", "start_stack", "stop_stack",
        "park",
    ]
    # goto got RA in hours, Dec in degrees (and the target name, issue 10)
    assert dev.calls_of("goto")[0][1:3] == (5.5, -5.0)
    assert dev.calls_of("goto")[1][1:3] == (10.0, 20.0)


# --- error policy with the FakeDevice ----------------------------------------------


def test_fake_device_goto_error_skip_then_abort(project):
    dev = FakeDevice()
    dev.goto_error = RuntimeError("goto boom")
    ex = NightExecutor(dev, two_target_queue(), project, frames_per_target=1)
    snap = ex.run(timeout=5)
    assert snap.state == ExecutorState.DONE  # skip: both entries fail, night ends DONE
    assert any("goto boom" in e.detail for e in ex.log)
    assert not dev.calls_of("start_stack")
    assert dev.calls_of("park")

    dev2 = FakeDevice()
    dev2.goto_error = RuntimeError("goto boom")
    ex2 = NightExecutor(
        dev2, two_target_queue(), project, frames_per_target=1, error_policy="abort"
    )
    snap2 = ex2.run(timeout=5)
    assert snap2.state == ExecutorState.ABORTED
    assert snap2.error is not None and "goto boom" in snap2.error
    assert len(dev2.calls_of("goto")) == 1  # aborted on the first entry
    assert not dev2.calls_of("park")


def test_fake_device_stack_error_skips_only_the_failing_entry(project):
    dev = FakeDevice()
    dev.stack_errors = [RuntimeError("stack boom")]
    ex = NightExecutor(dev, two_target_queue(), project, duration_per_target_s=0.05)
    snap = ex.run(timeout=5)
    assert snap.state == ExecutorState.DONE
    assert len(dev.calls_of("goto")) == 2
    assert len(dev.calls_of("start_stack")) == 2  # retried on the next entry
    assert any("stack boom" in e.detail for e in ex.log)


# --- run / wait / start semantics ----------------------------------------------------


def test_start_twice_raises_state_error(project):
    dev = FakeDevice()
    ex = NightExecutor(dev, two_target_queue(), project, frames_per_target=1000)
    assert ex.start() is not None
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    with pytest.raises(ExecutorStateError):
        ex.start()
    ex.stop()
    assert ex.wait(timeout=5)
    assert ex.snapshot.state == ExecutorState.DONE


def test_run_with_timeout_returns_early_and_session_stays_open(project):
    dev = FakeDevice()
    ex = NightExecutor(dev, PlanQueue([coord_entry(1.0, 2.0)]), project, frames_per_target=1000)
    snap = ex.run(timeout=0.3)  # worker still stacking -> early return
    assert snap.state == ExecutorState.STACKING
    assert get_open_session(project) is not None  # not closed while in flight
    ex.stop()
    assert ex.wait(timeout=5)
    assert ex.snapshot.state == ExecutorState.DONE
    assert get_open_session(project) is None


def test_wait_timeout_returns_false_then_closes_after_stop(project):
    dev = FakeDevice()
    ex = NightExecutor(dev, PlanQueue([coord_entry(1.0, 2.0)]), project, frames_per_target=1000)
    ex.start()
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    assert ex.wait(timeout=0.2) is False
    assert get_open_session(project) is not None
    ex.stop()
    assert ex.wait(timeout=5) is True
    assert get_open_session(project) is None


def test_empty_queue_done_immediately_with_session_and_park(device, project):
    ex = NightExecutor(device, PlanQueue(), project)
    snap = ex.run(timeout=10)
    assert snap.state == ExecutorState.DONE
    assert device.state().mode == "parked"
    sessions = list_sessions(project)
    assert len(sessions) == 1 and sessions[0].ended_at is not None
    assert get_open_session(project) is None


def test_session_open_failure_aborts_without_touching_the_open_session(project):
    existing = start_session(project)  # a session is already open
    dev = FakeDevice()
    ex = NightExecutor(dev, two_target_queue(), project, frames_per_target=1)
    snap = ex.run(timeout=5)  # start() fails -> ABORTED, no worker
    assert snap.state == ExecutorState.ABORTED
    assert snap.error is not None and "SessionOpenError" in snap.error
    assert ex.session is None
    assert not dev.calls  # the device was never driven
    open_session = get_open_session(project)
    assert open_session is not None and open_session.id == existing  # untouched
    close_session(project, existing)


# --- ticket 05: resuming a stale open session ---------------------------------------


def test_resume_session_reattaches_the_open_session(project):
    """``resume_session=<id>`` adopts the already-open session: no new row is
    inserted, the run continues in it, and it is closed when the run ends."""
    stale = start_session(project, started_at="2026-11-20T22:00:00+00:00")
    dev = FakeDevice()
    ex = NightExecutor(
        dev,
        PlanQueue([coord_entry(1.0, 2.0)]),
        project,
        frames_per_target=1,
        resume_session=stale,
    )
    assert ex.resume_session == stale
    worker = ex.start()
    assert worker is not None
    assert ex.session == stale  # reattached, not a new session
    assert len(list_sessions(project)) == 1
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    dev.emit_progress(1)
    assert ex.wait(timeout=5)
    assert ex.snapshot.state == ExecutorState.DONE
    sessions = list_sessions(project)
    assert len(sessions) == 1
    assert sessions[0].id == stale and sessions[0].ended_at is not None
    assert get_open_session(project) is None
    assert any("resumed" in event.detail for event in ex.log)


def test_resume_session_that_is_not_open_aborts(project):
    """Resuming a session that is not open fails like a session-open failure:
    ABORTED with the error, no worker, the device never driven."""
    dev = FakeDevice()
    ex = NightExecutor(
        dev, PlanQueue([coord_entry(1.0, 2.0)]), project, resume_session="missing-session"
    )
    snap = ex.run(timeout=5)
    assert snap.state == ExecutorState.ABORTED
    assert snap.error is not None and "missing-session" in snap.error
    assert ex.session is None
    assert not dev.calls
    assert get_open_session(project) is None


def test_resume_session_validation(project):
    dev = FakeDevice()
    with pytest.raises(ValueError, match="resume_session"):
        NightExecutor(dev, PlanQueue(), project, resume_session="")
    with pytest.raises(ValueError, match="resume_session"):
        NightExecutor(dev, PlanQueue(), project, resume_session=42)  # type: ignore[arg-type]


def test_invoke_on_main_marshals_listener_notifications(project):
    dev = FakeDevice()
    pending: list = []

    def invoke(callback):
        pending.append(callback)  # queue instead of running (like GLib.idle_add)

    snaps: list[ExecutorSnapshot] = []
    ex = NightExecutor(
        dev, PlanQueue([coord_entry(1.0, 2.0)]), project, duration_per_target_s=0.05,
        invoke_on_main=invoke,
    )
    ex.add_listener(snaps.append)
    snap = ex.run(timeout=5)
    assert snap.state == ExecutorState.DONE
    assert pending, "notifications were marshalled"
    assert not snaps, "nothing delivered before the main loop drains"
    for callback in pending:
        callback()
    assert snaps and snaps[-1].state == ExecutorState.DONE


# --- listeners / log / validation / API ----------------------------------------------


def test_add_remove_listener_is_idempotent(project):
    dev = FakeDevice()
    ex = NightExecutor(dev, two_target_queue(), project)
    listener = lambda snapshot: None
    ex.add_listener(listener)
    ex.add_listener(listener)
    assert ex.listeners == (listener,)
    ex.remove_listener(listener)
    assert ex.listeners == ()
    ex.remove_listener(listener)  # no error


def test_log_is_a_tuple_of_frozen_events(project):
    dev = FakeDevice()
    ex = NightExecutor(dev, PlanQueue([coord_entry(1.0, 2.0)]), project, duration_per_target_s=0.05)
    ex.run(timeout=5)
    log = ex.log
    assert isinstance(log, tuple) and log
    assert all(isinstance(e, ExecutorEvent) for e in log)
    assert all(isinstance(e.ts, float) and isinstance(e.state, ExecutorState) for e in log)
    assert any("opened" in e.detail for e in log)
    assert any("closed" in e.detail for e in log)
    assert log[-1].state == ExecutorState.DONE


def test_constructor_validation(project):
    dev = FakeDevice()
    q = two_target_queue()
    with pytest.raises(ValueError):
        NightExecutor(dev, q, project, error_policy="explode")
    with pytest.raises(ValueError):
        NightExecutor(dev, q, project, frames_per_target=0)
    with pytest.raises(ValueError):
        NightExecutor(dev, q, project, frames_per_target=1.5)
    with pytest.raises(ValueError):
        NightExecutor(dev, q, project, duration_per_target_s=-1.0)
    with pytest.raises(ValueError):
        NightExecutor(dev, q, project, poll_interval_s=0)
    with pytest.raises(ValueError):
        NightExecutor(dev, object(), project)  # no entries tuple
    # valid construction exposes the config
    ex = NightExecutor(
        dev, q, project, frames_per_target=3, duration_per_target_s=10.0,
        error_policy=ERROR_POLICY_ABORT, park_at_end=False,
    )
    assert ex.error_policy == ERROR_POLICY_ABORT
    assert ex.park_at_end is False
    assert ex.device is dev and ex.queue is q and ex.project is project
    assert ex.state == ExecutorState.IDLE
    assert ex.snapshot.state == ExecutorState.IDLE
    assert ex.snapshot.session is None


def test_public_api_exported_from_observing_package():
    for name in (
        "EXECUTOR_STATES", "ERROR_POLICIES", "ERROR_POLICY_ABORT", "ERROR_POLICY_SKIP",
        "ExecutorEvent", "ExecutorListener", "ExecutorSnapshot", "ExecutorState",
        "ExecutorStateError", "NightExecutor",
    ):
        assert name in observing.__all__
        assert getattr(observing, name) is not None
    assert set(EXECUTOR_STATES) == set(ExecutorState)
    assert ERROR_POLICIES == (ERROR_POLICY_SKIP, ERROR_POLICY_ABORT)
    assert {s.value for s in ExecutorState} == {
        "idle", "resolving", "slewing", "stacking", "paused", "done", "aborted",
    }


def test_executor_module_imports_no_gi_and_package_stays_link_free():
    code = (
        "import sys;"
        "import seestar_controller.observing;"
        "assert 'gi' not in sys.modules, 'gi imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.observing.executor' not in sys.modules, "
        "'executor imported eagerly';"
        "from seestar_controller.observing import NightExecutor, ExecutorState;"
        "assert 'gi' not in sys.modules, 'executor pulled in gi';"
        "assert 'seestar_controller.link' in sys.modules, 'lazy executor export works';"
        "assert ExecutorState.DONE.value == 'done';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


# --- issue 11: the scope was left stacking — recovery + plain-language errors ---------


def test_plain_rpc_message_maps_codes_259_and_203_and_ignores_others():
    busy_259 = plain_rpc_message(rpc_error("iscope_start_stack", 259, "already running"))
    busy_203 = plain_rpc_message(rpc_error("scope_park", 203, "busy"))
    assert busy_259 is not None and "code 259" in busy_259 and "stop stacking" in busy_259
    assert busy_203 is not None and "code 203" in busy_203 and "stop stacking" in busy_203
    # any other code / error type is not a known state conflict
    assert plain_rpc_message(rpc_error("scope_goto", 108, "wrong type")) is None
    assert plain_rpc_message(rpc_error("get_albums", 204, "page out of range")) is None
    assert plain_rpc_message(RuntimeError("boom")) is None


def test_scope_already_stacking_stops_the_stale_stack_before_start_stack(project):
    """Issue 11: ``device.state().mode`` reports stacking (a previous run died
    mid-stack) — ``stop_stack`` is called BEFORE ``start_stack`` and the entry
    proceeds normally."""
    dev = FakeDevice()
    dev.mode = "stacking"  # left stacking by a run that died with connection lost
    ex = NightExecutor(dev, PlanQueue([coord_entry(1.0, 2.0)]), project, duration_per_target_s=0.05)
    snap = ex.run(timeout=5)
    assert snap.state == ExecutorState.DONE
    assert snap.error is None
    kinds = [c[0] for c in dev.calls]
    assert kinds[:3] == ["goto", "stop_stack", "start_stack"]
    assert kinds.count("start_stack") == 1  # no retry needed after the pre-stop
    assert any("still stacking" in e.detail for e in ex.log)
    assert dev.calls_of("park")  # the run completed and parked


def test_start_stack_code_259_is_recovered_with_stop_and_single_retry(project):
    """Issue 11: ``start_stack`` fails once with code 259 — the executor stops
    the stale stack and retries ``start_stack`` once; the entry proceeds."""
    dev = FakeDevice()
    dev.stack_errors = [rpc_error("iscope_start_stack", 259, "already running", req_id=661)]
    ex = NightExecutor(dev, PlanQueue([coord_entry(1.0, 2.0)]), project, duration_per_target_s=0.05)
    snap = ex.run(timeout=5)
    assert snap.state == ExecutorState.DONE
    assert snap.error is None
    kinds = [c[0] for c in dev.calls]
    assert kinds[:4] == ["goto", "start_stack", "stop_stack", "start_stack"]
    assert any("still stacking" in e.detail for e in ex.log)  # logged in plain words
    assert dev.calls_of("park")


def test_start_stack_code_259_twice_errors_the_entry_with_plain_language(project):
    """Issue 11: ``start_stack`` fails with code 259 twice (the stop did not
    clear the state) — the entry errors (skip policy) with the plain-language
    message, not just ``code=259``."""
    dev = FakeDevice()
    err = rpc_error("iscope_start_stack", 259, "already running", req_id=661)
    dev.stack_errors = [err, err]
    snaps: list[ExecutorSnapshot] = []
    ex = NightExecutor(dev, PlanQueue([coord_entry(1.0, 2.0)]), project, duration_per_target_s=0.05)
    ex.add_listener(snaps.append)
    snap = ex.run(timeout=5)
    assert snap.state == ExecutorState.DONE  # skip policy: entry skipped, night ends
    assert len(dev.calls_of("start_stack")) == 2  # the original call + the single retry
    assert len(dev.calls_of("stop_stack")) == 1  # the recovery stop
    assert any(
        "error on entry 0" in e.detail
        and "scope is busy (code 259)" in e.detail
        and "stop stacking on the scope first" in e.detail
        for e in ex.log
    )
    assert any(s.error is not None and "scope is busy (code 259)" in s.error for s in snaps)
    assert dev.calls_of("park")


def test_start_stack_other_error_code_is_not_recovered(project):
    """Issue 11: only code 259 is recoverable — any other code keeps the
    existing error policy (no stop/retry, the entry errors as before)."""
    dev = FakeDevice()
    dev.stack_errors = [rpc_error("iscope_start_stack", 108, "wrong type")]
    ex = NightExecutor(dev, PlanQueue([coord_entry(1.0, 2.0)]), project, duration_per_target_s=0.05)
    snap = ex.run(timeout=5)
    assert snap.state == ExecutorState.DONE  # skip policy
    assert len(dev.calls_of("start_stack")) == 1  # no retry
    assert not dev.calls_of("stop_stack")  # no recovery stop
    assert any("code=108" in e.detail and "scope is busy" not in e.detail for e in ex.log)

    dev2 = FakeDevice()
    dev2.stack_errors = [rpc_error("iscope_start_stack", 108, "wrong type")]
    ex2 = NightExecutor(
        dev2, PlanQueue([coord_entry(1.0, 2.0)]), project, duration_per_target_s=0.05,
        error_policy="abort",
    )
    snap2 = ex2.run(timeout=5)
    assert snap2.state == ExecutorState.ABORTED  # abort policy unchanged
    assert snap2.error is not None and "code=108" in snap2.error
    assert "scope is busy" not in snap2.error  # no plain-language mapping for 108
    assert len(dev2.calls_of("start_stack")) == 1
    assert not dev2.calls_of("park")


def test_park_code_203_surfaces_plain_language_in_the_log(project):
    """Issue 11: ``scope_park`` fails with code 203 while the scope is still
    stacking — the park failure is logged in plain language (the run itself
    still ends DONE)."""
    dev = FakeDevice()
    dev.park_errors = [rpc_error("scope_park", 203, "busy", req_id=663)]
    ex = NightExecutor(dev, PlanQueue([coord_entry(1.0, 2.0)]), project, duration_per_target_s=0.05)
    snap = ex.run(timeout=5)
    assert snap.state == ExecutorState.DONE  # a failed park does not abort the run
    assert len(dev.calls_of("park")) == 1
    assert any(
        "park failed" in e.detail
        and "scope is busy (code 203)" in e.detail
        and "stop stacking on the scope first" in e.detail
        for e in ex.log
    )
