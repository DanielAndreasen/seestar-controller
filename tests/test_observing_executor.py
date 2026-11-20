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
from seestar_controller.link.device import Device, StackingProgress
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
        self._lock = threading.Lock()

    def goto(self, ra, dec, *, timeout=None):
        with self._lock:
            self.calls.append(("goto", ra, dec))
            error = self.goto_error
        if error is not None:
            raise error
        return SimpleNamespace(mode="idle", target=(ra, dec))

    def park(self):
        with self._lock:
            self.calls.append(("park",))
        return SimpleNamespace(mode="parked")

    def start_stack(self, exposure, gain, mosaic=False, *, timeout=None):
        with self._lock:
            self.calls.append(("start_stack", exposure, gain, mosaic))
            error = self.stack_errors.pop(0) if self.stack_errors else None
        if error is not None:
            raise error
        return SimpleNamespace(mode="stacking")

    def stop_stack(self, *, timeout=None):
        with self._lock:
            self.calls.append(("stop_stack",))
        return SimpleNamespace(mode="idle")

    def state(self):
        return SimpleNamespace(mode="idle")

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
    # goto got RA in hours, Dec in degrees
    assert dev.calls_of("goto")[0][1:] == (5.5, -5.0)
    assert dev.calls_of("goto")[1][1:] == (10.0, 20.0)


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
