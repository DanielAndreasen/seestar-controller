"""Observing ticket 03 — the Observe-view controller (headless, pure layer).

:class:`~seestar_controller.observing.observe_controller.ObserveController`
binds the pieces the Observe view needs — the ui
:class:`~seestar_controller.ui.connection.ConnectionController`, the shared
:class:`~seestar_controller.observing.queue_controller.PlanQueueController`,
a :class:`~seestar_controller.observing.executor.NightExecutor` factory, a
preview-stream factory and the notification bus — and emits frozen
:class:`~seestar_controller.observing.observe_controller.ObserveViewState`
snapshots (executor transition, preview frame available, queue change) to the
view listeners, all marshalled through ``invoke_on_main``.

Device doubles, per docs/wiki/assumptions.md (no real scope, user AFK — all
link work is mock-only):

- a **FakeDevice** (the executor facade shape + a ``transport.host``) and a
  **FakePreviewStream** (the
  :class:`~seestar_controller.link.preview.PreviewStream` shape) for the
  deterministic controller tests — start/pause/skip/stop/park, preview frames
  + throttle, DONE/ABORTED stopping the preview, notifications,
  ``invoke_on_main`` marshalling;
- the **real Device facade + real PreviewStream against the mock Scope** on
  127.0.0.1 (conftest ``transport`` / ``mock_scope`` fixtures) for one
  end-to-end run: start the night, watch preview frames + progress flow, DONE
  stops the preview.

Plus a subprocess test enforcing the no-``gi`` boundary of the module (the
package itself stays lazy until the observe names are touched).
"""

import dataclasses
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

import seestar_controller.observing as observing
from seestar_controller.catalog.coords import J2000Coord
from seestar_controller.link.device import Device, StackingProgress
from seestar_controller.link.image import Frame
from seestar_controller.link.preview import PreviewFrame, PreviewStream, decode_payload
from seestar_controller.observing import (
    ExecutorState,
    ObserveController,
    ObserveStateError,
    ObserveViewState,
    PlanQueueController,
    QueueEntry,
    default_preview_stream_factory,
    format_elapsed,
    format_target,
)
from seestar_controller.observing.observe_controller import (
    DEFAULT_PREVIEW_MAX_FPS,
    OBSERVE_SOURCE,
)
from seestar_controller.store.project import create_project
from seestar_controller.ui.connection import ConnectionController, ConnectionStatus
from seestar_controller.ui.notifications import NotificationBus, NotificationSeverity
from seestar_controller.ui.settings import SettingsController
from seestar_controller.ui.state import AppState


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def coord_entry(ra_hours: float, dec_deg: float, **kwargs) -> QueueEntry:
    return QueueEntry(coord=J2000Coord.from_hours(ra_hours, dec_deg), **kwargs)


def make_preview_frame(frame_id: int, width: int = 4, height: int = 3) -> PreviewFrame:
    """A decoded PreviewFrame over a small raw wire frame (grayscale decode)."""
    size = width * height
    payload = bytes([frame_id % 256]) * size
    frame = Frame(code=0, id=frame_id, width=width, height=height, size=size, payload=payload)
    return PreviewFrame(
        source="tele", image=decode_payload(frame), frame=frame, timestamp=time.monotonic()
    )


class FakeDevice:
    """Duck-typed stand-in for the link Device facade — no network, no gi.

    The executor-test shape (goto/park/start_stack/stop_stack + event and
    stacking-progress handlers, scripted errors, ``calls_of``) plus a
    ``transport.host`` so the observe controller's preview-host fallback has
    something to read.
    """

    def __init__(self, host: str = "10.9.8.7"):
        self.calls: list[tuple] = []
        self.transport = SimpleNamespace(host=host)
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


class FakePreviewStream:
    """Duck-typed stand-in for :class:`~seestar_controller.link.preview.PreviewStream`.

    ``add_callback`` / ``start(tele=, wide=)`` / ``stop()`` / ``running``;
    :meth:`emit` dispatches a frame to the callbacks (optionally on a fresh
    thread, like the real dispatcher thread). ``fail_on_start`` makes
    ``start`` raise, simulating an unreachable image socket.
    """

    def __init__(self, fail_on_start: bool = False):
        self.callbacks: list = []
        self.running = False
        self.fail_on_start = fail_on_start
        self.host: str | None = None
        self.start_calls: list[tuple] = []
        self.stop_calls = 0

    def add_callback(self, callback):
        self.callbacks.append(callback)

    def remove_callback(self, callback):
        if callback in self.callbacks:
            self.callbacks.remove(callback)

    def start(self, tele: bool = True, wide: bool = False):
        self.start_calls.append((tele, wide))
        if self.fail_on_start:
            raise RuntimeError("image socket unreachable")
        self.running = True

    def stop(self):
        self.stop_calls += 1
        self.running = False

    def emit(self, frame, *, threaded: bool = False):
        if threaded:
            threading.Thread(target=self._dispatch, args=(frame,), daemon=True).start()
        else:
            self._dispatch(frame)

    def _dispatch(self, frame):
        for callback in list(self.callbacks):
            callback(frame)


def make_stream_factory(streams: list, **stream_kwargs):
    """A preview-stream factory collecting every built stream in ``streams``."""

    def factory(device, host):
        stream = FakePreviewStream(**stream_kwargs)
        stream.host = host
        streams.append(stream)
        return stream

    return factory


def build_harness(tmp_path, *, invoke_on_main=None, preview_max_fps=DEFAULT_PREVIEW_MAX_FPS,
                  stream_kwargs=None, connect: bool = True):
    """The app-level controllers with fakes — headless, no network, no gi.

    Mirrors what ``SeestarApplication`` owns (settings -> connection -> bus,
    plus the shared queue controller and the observe controller) with a
    FakeDevice behind the connection's device factory.
    """
    state = AppState(tmp_path / "settings.json")
    state.set_setting("scope_ip", "10.9.8.7")
    settings = SettingsController(state)
    device = FakeDevice()
    connection = ConnectionController(settings, device_factory=lambda ip, key_path: device)
    bus = NotificationBus()
    queue_controller = PlanQueueController()
    streams: list[FakePreviewStream] = []
    controller = ObserveController(
        connection,
        queue_controller,
        bus,
        preview_stream_factory=make_stream_factory(streams, **(stream_kwargs or {})),
        invoke_on_main=invoke_on_main,
        preview_max_fps=preview_max_fps,
    )
    if connect:
        connection.connect()
        assert connection.wait(2.0)
        assert connection.status == ConnectionStatus.CONNECTED
    return SimpleNamespace(
        tmp_path=tmp_path,
        state=state,
        settings=settings,
        device=device,
        connection=connection,
        bus=bus,
        queue_controller=queue_controller,
        streams=streams,
        controller=controller,
    )


@pytest.fixture()
def harness(tmp_path):
    return build_harness(tmp_path)


@pytest.fixture()
def project(tmp_path):
    p = create_project("Test Night", root=tmp_path / "projects")
    yield p
    p.close()


# --- guards ---------------------------------------------------------------------------


def test_start_night_requires_connection(tmp_path, project):
    h = build_harness(tmp_path, connect=False)
    with pytest.raises(ObserveStateError, match="not connected"):
        h.controller.start_night(project)


def test_start_night_requires_project(harness, project):
    with pytest.raises(ObserveStateError, match="no project"):
        harness.controller.start_night()
    harness.controller.set_project(project)
    assert harness.controller.project is project
    harness.queue_controller.add_entry(coord_entry(5.5, -5.0))
    ex = harness.controller.start_night(frames_per_target=1)
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    harness.device.emit_progress(1)
    assert ex.wait(timeout=5)
    assert ex.snapshot.state == ExecutorState.DONE


def test_start_night_while_running_raises(harness, project):
    harness.queue_controller.add_entry(coord_entry(5.5, -5.0))
    ex = harness.controller.start_night(project, duration_per_target_s=30.0)
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    with pytest.raises(ObserveStateError, match="already running"):
        harness.controller.start_night(project)
    harness.controller.stop()
    assert ex.wait(timeout=5)
    assert ex.snapshot.state == ExecutorState.DONE


def test_park_requires_connection(tmp_path):
    h = build_harness(tmp_path, connect=False)
    with pytest.raises(ObserveStateError, match="not connected"):
        h.controller.park()


# --- a full night with the fakes --------------------------------------------------------


def test_full_night_preview_frames_progress_and_done_stops_preview(harness, project):
    qc = harness.queue_controller
    qc.add_entry(coord_entry(5.5, -5.0))
    qc.add_entry(coord_entry(10.0, 20.0))
    states: list[ObserveViewState] = []
    frames: list = []
    harness.controller.add_listener(states.append)
    harness.controller.on_preview_frame(frames.append)

    ex = harness.controller.start_night(project, frames_per_target=2)
    stream = harness.streams[0]
    assert stream.host == "10.9.8.7"  # the connection's scope_ip
    assert stream.start_calls == [(True, False)]
    assert stream.running
    assert harness.controller.preview_running

    # drive the night: preview frames + stacking progress until DONE
    # (emit_progress takes the CUMULATIVE stacked-frame count)
    frame_id = 0
    stacked = 0
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and ex.state not in (
        ExecutorState.DONE,
        ExecutorState.ABORTED,
    ):
        stream.emit(make_preview_frame(frame_id))
        frame_id += 1
        stacked += 1
        harness.device.emit_progress(stacked)
        time.sleep(0.01)
    assert ex.wait(timeout=5)
    assert ex.snapshot.state == ExecutorState.DONE

    # both targets were visited, parked at the end
    assert sorted(c[1] for c in harness.device.calls_of("goto")) == [5.5, 10.0]
    assert harness.device.calls_of("park")

    # preview frames flowed to the on_preview_frame listener; latest_frame is
    # the last emitted one (updated even when the notification is throttled)
    assert frames, "no preview frames delivered"
    assert harness.controller.latest_frame is not None
    assert harness.controller.latest_frame.id == frame_id - 1
    assert isinstance(harness.controller.latest_frame.image, np.ndarray)

    # DONE stopped the preview
    assert stream.stop_calls >= 1
    assert not stream.running
    assert harness.controller.stream is None
    assert not harness.controller.preview_running

    # the view-state sequence: active start, STACKING with progress, final DONE
    assert states[0].night_active
    stacking = [s for s in states if s.executor_state == ExecutorState.STACKING]
    assert stacking, "no STACKING snapshot emitted"
    assert stacking[-1].entry is not None
    assert stacking[-1].frames_stacked >= 1
    assert stacking[-1].elapsed_s >= 0.0
    assert stacking[-1].queue_index in (0, 1)
    assert stacking[-1].session == ex.session
    assert states[-1].executor_state == ExecutorState.DONE
    assert not states[-1].night_active
    assert not states[-1].preview_running
    assert states[-1].preview_frame is not None
    assert len(states[-1].queue_entries) == 2

    # notifications: night started + finished, from the observe source
    titles = [(n.severity, n.title, n.source) for n in harness.bus.log()]
    assert (NotificationSeverity.INFO, "Night started", OBSERVE_SOURCE) in titles
    assert (NotificationSeverity.INFO, "Night finished", OBSERVE_SOURCE) in titles


def test_progress_snapshot_fields(harness, project):
    harness.queue_controller.add_entry(coord_entry(5.5, -5.0))
    states: list[ObserveViewState] = []
    harness.controller.add_listener(states.append)
    ex = harness.controller.start_night(project, frames_per_target=3)
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    harness.device.emit_progress(2)
    assert wait_until(lambda: any(s.frames_stacked == 2 for s in states))
    snap = [s for s in states if s.frames_stacked == 2][-1]
    assert snap.queue_index == 0
    assert snap.entry is not None and snap.entry.coord == J2000Coord.from_hours(5.5, -5.0)
    assert isinstance(snap.elapsed_s, float) and snap.elapsed_s >= 0.0
    assert snap.error is None
    assert snap.session is not None
    assert len(snap.queue_entries) == 1
    assert snap.night_active
    harness.device.emit_progress(3)  # cumulative: reaches frames_per_target=3
    assert ex.wait(timeout=5)
    assert ex.snapshot.state == ExecutorState.DONE


def test_pause_resume_skip_stop_passthroughs_drive_the_executor(harness, project):
    qc = harness.queue_controller
    qc.add_entry(coord_entry(5.5, -5.0))
    qc.add_entry(coord_entry(10.0, 20.0))
    ex = harness.controller.start_night(project, frames_per_target=100)
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)

    harness.controller.pause()
    assert wait_until(lambda: ex.state == ExecutorState.PAUSED)
    assert harness.controller.snapshot.executor_state == ExecutorState.PAUSED
    assert harness.device.calls_of("stop_stack"), "pause stops the current Stack"

    harness.controller.resume()
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)

    harness.controller.skip()
    assert wait_until(lambda: ex.snapshot.queue_index == 1)

    harness.controller.stop()
    assert ex.wait(timeout=5)
    assert ex.snapshot.state == ExecutorState.DONE

    # passthroughs are no-ops once the night is over
    harness.controller.pause()
    harness.controller.resume()
    harness.controller.skip()
    harness.controller.stop()
    assert ex.snapshot.state == ExecutorState.DONE


def test_park_calls_device_park_and_notifies(harness):
    harness.controller.park()
    assert harness.device.calls_of("park") == [("park",)]
    notes = harness.bus.log()
    assert any(
        n.title == "Scope parked"
        and n.severity == NotificationSeverity.INFO
        and n.source == OBSERVE_SOURCE
        for n in notes
    )


def test_aborted_night_stops_preview_and_pushes_error(harness, project):
    harness.device.goto_error = RuntimeError("slew failed")
    harness.queue_controller.add_entry(coord_entry(5.5, -5.0))
    states: list[ObserveViewState] = []
    harness.controller.add_listener(states.append)
    ex = harness.controller.start_night(project, error_policy="abort")
    assert ex.wait(timeout=5)
    assert ex.snapshot.state == ExecutorState.ABORTED
    assert ex.snapshot.error
    stream = harness.streams[0]
    assert stream.stop_calls >= 1 and not stream.running
    assert harness.controller.stream is None
    assert states[-1].executor_state == ExecutorState.ABORTED
    assert states[-1].error
    notes = harness.bus.log()
    assert any(
        n.severity == NotificationSeverity.ERROR and n.title == "Night aborted" for n in notes
    )


# --- preview frames ----------------------------------------------------------------------


def test_preview_notifications_throttled_but_latest_frame_always_updated(tmp_path, project):
    h = build_harness(tmp_path, preview_max_fps=1.0)
    h.queue_controller.add_entry(coord_entry(5.5, -5.0))
    frames: list = []
    h.controller.on_preview_frame(frames.append)
    ex = h.controller.start_night(project, duration_per_target_s=30.0)
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    stream = h.streams[0]
    stream.emit(make_preview_frame(1))
    stream.emit(make_preview_frame(2))
    stream.emit(make_preview_frame(3))
    assert len(frames) == 1, "max_fps=1.0 must throttle the 2nd/3rd quick frame"
    assert frames[0].id == 1
    assert h.controller.latest_frame.id == 3, "latest_frame updates even when throttled"
    h.controller.stop()
    assert ex.wait(timeout=5)


def test_raw_wire_frame_is_decoded_via_decode_payload(harness, project):
    harness.queue_controller.add_entry(coord_entry(5.5, -5.0))
    frames: list = []
    harness.controller.on_preview_frame(frames.append)
    ex = harness.controller.start_night(project, duration_per_target_s=30.0)
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    raw = Frame(code=0, id=99, width=4, height=3, size=12, payload=bytes(range(12)))
    harness.streams[0].emit(raw)
    pf = harness.controller.latest_frame
    assert isinstance(pf, PreviewFrame)
    assert pf.frame is raw
    assert pf.image.shape == (3, 4)  # width*height bytes -> grayscale (h, w)
    assert frames[-1] is pf
    harness.controller.stop()
    assert ex.wait(timeout=5)


def test_preview_frame_passthrough_and_unknown_objects_ignored(harness, project):
    harness.queue_controller.add_entry(coord_entry(5.5, -5.0))
    frames: list = []
    harness.controller.on_preview_frame(frames.append)
    ex = harness.controller.start_night(project, duration_per_target_s=30.0)
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    pf = make_preview_frame(7)
    harness.streams[0].emit(pf)
    assert frames[-1] is pf, "a PreviewFrame is passed through unchanged"
    harness.streams[0].emit(object())  # unknown object: ignored, no crash
    assert harness.controller.latest_frame is pf
    harness.controller.stop()
    assert ex.wait(timeout=5)


def test_preview_start_failure_warns_but_the_night_still_runs(tmp_path, project):
    h = build_harness(tmp_path, stream_kwargs={"fail_on_start": True})
    h.queue_controller.add_entry(coord_entry(5.5, -5.0))
    ex = h.controller.start_night(project, frames_per_target=1)
    assert h.controller.stream is None
    assert not h.controller.preview_running
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    h.device.emit_progress(1)
    assert ex.wait(timeout=5)
    assert ex.snapshot.state == ExecutorState.DONE
    notes = h.bus.log()
    assert any(
        n.severity == NotificationSeverity.WARNING and n.title == "Preview unavailable"
        for n in notes
    )


def test_stop_preview_is_idempotent(harness, project):
    harness.queue_controller.add_entry(coord_entry(5.5, -5.0))
    ex = harness.controller.start_night(project, duration_per_target_s=30.0)
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    stream = harness.streams[0]
    harness.controller.stop_preview()
    assert stream.stop_calls == 1 and not stream.running
    assert harness.controller.stream is None
    harness.controller.stop_preview()  # no-op, no crash
    assert stream.stop_calls == 1
    harness.controller.stop()
    assert ex.wait(timeout=5)


# --- listeners / emission / marshalling ---------------------------------------------------


def test_queue_change_emits_view_state(harness):
    states: list[ObserveViewState] = []
    harness.controller.add_listener(states.append)
    harness.queue_controller.add_entry(coord_entry(1.0, 2.0))
    assert states and len(states[-1].queue_entries) == 1
    harness.queue_controller.add_entry(coord_entry(3.0, 4.0))
    assert len(states[-1].queue_entries) == 2
    # remove_listener stops the emissions
    harness.controller.remove_listener(states.append)
    n = len(states)
    harness.queue_controller.add_entry(coord_entry(5.0, 6.0))
    assert len(states) == n


def test_view_state_is_frozen_and_idle_by_default(harness):
    snap = harness.controller.snapshot
    assert isinstance(snap, ObserveViewState)
    assert snap.executor_state == ExecutorState.IDLE
    assert snap.queue_index is None and snap.entry is None
    assert snap.frames_stacked == 0 and snap.elapsed_s == 0.0
    assert snap.error is None and snap.session is None
    assert snap.preview_frame is None
    assert not snap.preview_running and not snap.night_active
    assert snap.queue_entries == ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.frames_stacked = 99  # type: ignore[misc]


def test_listener_exceptions_do_not_break_others(harness):
    def bad(_state):
        raise RuntimeError("boom")

    good: list[ObserveViewState] = []
    harness.controller.add_listener(bad)
    harness.controller.add_listener(good.append)
    harness.queue_controller.add_entry(coord_entry(1.0, 2.0))
    assert good, "the good listener still fired after the bad one raised"


def test_callbacks_are_marshalled_via_invoke_on_main(tmp_path, project):
    pending: list = []

    def invoke(callback):
        pending.append(callback)

    h = build_harness(tmp_path, invoke_on_main=invoke)
    h.queue_controller.add_entry(coord_entry(5.5, -5.0))
    states: list[ObserveViewState] = []
    frames: list = []
    h.controller.add_listener(states.append)
    h.controller.on_preview_frame(frames.append)

    ex = h.controller.start_night(project, frames_per_target=1)
    # the stream callback arrives on a "dispatcher" thread and is marshalled
    h.streams[0].emit(make_preview_frame(1), threaded=True)
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    h.device.emit_progress(1)
    assert wait_until(lambda: ex.state == ExecutorState.DONE, timeout=10)

    # nothing from the worker/dispatcher threads was delivered yet — only the
    # direct start_night emission reached the listener
    assert frames == []
    assert len(states) == 1

    while pending:
        pending.pop(0)()
    assert frames and frames[-1].id == 1
    assert states[-1].executor_state == ExecutorState.DONE
    assert states[-1].preview_frame is not None


def test_on_preview_frame_decorator_form(harness, project):
    received: list = []

    @harness.controller.on_preview_frame
    def on_frame(frame):
        received.append(frame)

    harness.queue_controller.add_entry(coord_entry(5.5, -5.0))
    ex = harness.controller.start_night(project, duration_per_target_s=30.0)
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    harness.streams[0].emit(make_preview_frame(3))
    assert [f.id for f in received] == [3]
    harness.controller.stop()
    assert ex.wait(timeout=5)


# --- formatting helpers / factories / exports ---------------------------------------------


def test_format_target_and_elapsed():
    assert format_target(None) == "—"
    assert format_target(QueueEntry(name="M 42")) == "M 42 (unresolved)"
    coord = J2000Coord.from_hours(5.5881, -5.39114)
    assert format_target(QueueEntry(coord=coord)) == "RA 5.5881h Dec -5.3911°"
    resolved = QueueEntry.resolved("M 42", coord)
    assert format_target(resolved) == "M 42 — RA 5.5881h Dec -5.3911°"
    assert format_elapsed(0.0) == "0 s"
    assert format_elapsed(42.4) == "42 s"
    assert format_elapsed(-3.0) == "0 s"


def test_default_preview_stream_factory_builds_a_stream_from_the_host():
    stream = default_preview_stream_factory(SimpleNamespace(), "192.168.1.42")
    assert isinstance(stream, PreviewStream)
    assert stream.host == "192.168.1.42"
    assert stream.max_fps == DEFAULT_PREVIEW_MAX_FPS


def test_observe_names_are_lazy_exports_of_the_package():
    from seestar_controller.observing import observe_controller

    for name in (
        "ObserveController",
        "ObserveViewState",
        "ObserveStateError",
        "ObserveViewListener",
        "PreviewFrameListener",
        "ExecutorFactory",
        "PreviewStreamFactory",
        "InvokeOnMain",
        "default_executor_factory",
        "default_preview_stream_factory",
        "format_target",
        "format_elapsed",
        "OBSERVE_SOURCE",
        "DEFAULT_PREVIEW_MAX_FPS",
    ):
        assert name in observing.__all__
        assert getattr(observing, name) is getattr(observe_controller, name)
    assert observing._EXECUTOR_EXPORTS["ObserveController"] == "observe_controller"
    assert observing._WIDGET_EXPORTS["ObserveView"] == "observe_view"
    assert "ObserveView" in observing.__all__


def test_observe_controller_module_imports_no_gi_and_the_package_stays_lazy():
    code = (
        "import sys;"
        "import seestar_controller.observing;"
        "assert 'seestar_controller.observing.observe_controller' not in sys.modules, "
        "'observe_controller imported eagerly';"
        "assert 'seestar_controller.observing.observe_view' not in sys.modules, "
        "'observe_view imported eagerly';"
        "import seestar_controller.observing.observe_controller;"
        "assert 'gi' not in sys.modules, 'gi imported';"
        "assert 'seestar_controller.observing.observe_view' not in sys.modules, "
        "'observe_view imported by the pure module';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


# --- end-to-end against the real facade + real preview stream + mock Scope -----------------


def test_end_to_end_against_mock_scope_preview_and_progress_flow(
    mock_scope, transport, tmp_path
):
    mock_scope.slew_duration = 0.05
    mock_scope.stack_event_interval = 0.02
    device = Device.connect("127.0.0.1", transport=transport, goto_timeout=2.0)
    state = AppState(tmp_path / "settings.json")
    state.set_setting("scope_ip", "127.0.0.1")
    settings = SettingsController(state)
    connection = ConnectionController(settings, device_factory=lambda ip, key_path: device)
    bus = NotificationBus()
    queue_controller = PlanQueueController()
    queue_controller.add_entry(coord_entry(5.5, -5.0))
    queue_controller.add_entry(coord_entry(10.0, 20.0))

    def preview_factory(dev, host):
        assert host == "127.0.0.1"
        return PreviewStream(host, tele_port=mock_scope.tele_port, max_fps=50.0)

    controller = ObserveController(
        connection, queue_controller, bus, preview_stream_factory=preview_factory
    )
    states: list[ObserveViewState] = []
    frames: list = []
    controller.add_listener(states.append)
    controller.on_preview_frame(frames.append)

    connection.connect()
    assert connection.wait(5.0)
    assert connection.status == ConnectionStatus.CONNECTED

    project = create_project("Mock Night", root=tmp_path / "projects")
    try:
        ex = controller.start_night(project, frames_per_target=1)
        assert ex.wait(timeout=20)
        assert ex.snapshot.state == ExecutorState.DONE
        # both targets visited, parked at the end
        assert mock_scope.last_goto == {"ra": 10.0, "dec": 20.0}
        assert device.state().mode == "parked"
        # real preview frames flowed from the mock scope's image socket
        assert wait_until(lambda: len(frames) >= 1, timeout=5)
        latest = controller.latest_frame
        assert latest is not None
        assert isinstance(latest.image, np.ndarray) and latest.image.size > 0
        assert latest.source == "tele"
        # progress snapshots carried the current target + stacked frames
        stacking = [s for s in states if s.executor_state == ExecutorState.STACKING]
        assert stacking and stacking[-1].frames_stacked >= 1
        assert stacking[-1].entry is not None
        # DONE stopped the preview stream
        assert wait_until(lambda: not controller.preview_running, timeout=5)
        assert states[-1].executor_state == ExecutorState.DONE
        assert not states[-1].night_active
        assert states[-1].preview_frame is not None
        titles = [n.title for n in bus.log()]
        assert "Night started" in titles and "Night finished" in titles
    finally:
        project.close()
        device.disconnect()
