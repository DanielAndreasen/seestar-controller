"""Observing ticket 04 — session logging + frame downloads (headless, pure layer).

:class:`~seestar_controller.observing.capture.SessionCapture` runs alongside
the :class:`~seestar_controller.observing.executor.NightExecutor` during a
night: sub-frames/Stacks are downloaded from the device into the current
session as they arrive (dedupe by Scope frame id), and a per-session event
log (goto / stack start-stop / errors / frames stored, with timestamps) is
kept in memory and persisted as JSONL in the session directory. The
:class:`~seestar_controller.observing.observe_controller.ObserveController`
starts the capture on ``start_night`` and stops it on DONE/ABORTED; capture
errors become notifications and never kill the night.

Device doubles, per docs/wiki/assumptions.md (no real scope, user AFK — all
link work is mock-only):

- the **real Device facade against the mock Scope** on 127.0.0.1 (conftest
  ``transport`` / ``mock_scope`` fixtures) for the end-to-end run — the mock
  records a downloadable sub-frame per stacked sub-frame and one Stack on
  ``stop_stack``, so a 2-target night lands frames in the tmp project's
  session and the log shows goto/stack/frame events;
- a **FakeDevice** (executor facade shape + a scripted ``download_frames``
  writing real FITS via :func:`~seestar_controller.link.mock_scope.make_fits_bytes`)
  for the deterministic tests — worker loop with a queueing ``invoke_on_main``,
  download failure -> error event + notification + recovery, dedupe across a
  capture restart, device without ``download_frames``, and the
  ObserveController lifecycle.

Plus a subprocess test enforcing the no-``gi`` boundary of the capture module
(the package itself stays lazy until the capture names are touched).
"""

import dataclasses
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import seestar_controller.observing as observing
from seestar_controller.catalog.coords import J2000Coord
from seestar_controller.link.device import Device, StackingProgress
from seestar_controller.link.frames import (
    DownloadedFrame,
    FrameMeta,
    atomic_write_json,
    meta_to_sidecar,
)
from seestar_controller.link.mock_scope import make_fits_bytes
from seestar_controller.observing import (
    CAPTURE_SOURCE,
    KIND_CAPTURE,
    KIND_ERROR,
    KIND_EXECUTOR,
    KIND_FRAME,
    KIND_GOTO,
    KIND_SESSION,
    KIND_STACK,
    LOG_KINDS,
    LOG_SOURCE_CAPTURE,
    LOG_SOURCE_EXECUTOR,
    SESSION_LOG_FILE,
    ExecutorState,
    LogEvent,
    NightExecutor,
    ObserveController,
    SessionCapture,
    SessionLog,
    classify_executor_event,
    session_log_path,
)
from seestar_controller.observing.observe_controller import OBSERVE_SOURCE, default_capture_factory
from seestar_controller.observing.queue import PlanQueue, QueueEntry
from seestar_controller.observing.queue_controller import PlanQueueController
from seestar_controller.store.frames import get_frame_path, list_frames, store_frame
from seestar_controller.store.project import create_project
from seestar_controller.store.sessions import close_session, start_session
from seestar_controller.ui.connection import ConnectionController
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


def drain_until(pending: list, predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """Drain the queued ``invoke_on_main`` callbacks on the calling thread
    until ``predicate`` holds (or the timeout expires)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while pending:
            pending.pop(0)()
        if predicate():
            return True
        time.sleep(interval)
    while pending:
        pending.pop(0)()
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


class FakeDevice:
    """Duck-typed stand-in for the link Device facade — no network, no gi.

    The executor-test shape (goto/park/start_stack/stop_stack + event and
    stacking-progress handlers, ``calls_of``) plus a scripted
    ``download_frames(dest_dir, kind=, since=, timeout=)`` that writes real
    FITS files + sidecars into ``dest_dir`` and returns
    :class:`~seestar_controller.link.frames.DownloadedFrame`\\ s, like the
    real facade. ``auto_frames`` mimics the mock Scope: a producer thread
    records a sub-frame per stack tick while stacking, and ``stop_stack``
    records the final Stack.
    """

    def __init__(self, host: str = "10.9.8.7"):
        self.calls: list[tuple] = []
        self.transport = SimpleNamespace(host=host)
        self._handlers: dict[str, list] = {}
        self._progress_handlers: list = []
        self.frames: list[dict] = []
        self.download_error: Exception | None = None
        self.auto_frames = False
        self._stacking = False
        self._seq = 0
        self._lock = threading.Lock()

    # -- executor facade ------------------------------------------------------

    def goto(self, ra, dec, *, timeout=None):
        with self._lock:
            self.calls.append(("goto", ra, dec))
        return SimpleNamespace(mode="idle", target=(ra, dec))

    def park(self):
        with self._lock:
            self.calls.append(("park",))
        return SimpleNamespace(mode="parked")

    def start_stack(self, exposure, gain, mosaic=False, *, timeout=None):
        with self._lock:
            self.calls.append(("start_stack", exposure, gain, mosaic))
            self._stacking = True
        if self.auto_frames:
            def produce():
                while self._stacking:
                    self.add_frame("sub", exposure=exposure, gain=gain)
                    with self._lock:
                        count = sum(1 for r in self.frames if r["kind"] == "sub")
                    self.emit_progress(frames=count)  # the executor counts frames from these
                    time.sleep(0.02)

            threading.Thread(target=produce, daemon=True).start()
        return SimpleNamespace(mode="stacking")

    def stop_stack(self, *, timeout=None):
        with self._lock:
            self.calls.append(("stop_stack",))
            self._stacking = False
        if self.auto_frames:
            self.add_frame("stack", exposure=10.0, gain=80)
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

    # -- frame store + download -------------------------------------------------

    def add_frame(self, kind, *, exposure=10.0, gain=80, target=None, when=None):
        """Record one frame (real FITS bytes) and return its public record."""
        with self._lock:
            self._seq += 1
            seq = self._seq
            frame_id = f"{kind}-{seq:04d}"
            when = when or datetime.now(timezone.utc)
            fits = make_fits_bytes(
                when=when, exposure=exposure, gain=gain, target=target, seed=seq
            )
            record = {
                "id": frame_id,
                "kind": kind,
                "time": when.isoformat(),
                "epoch": when.timestamp(),
                "exposure": float(exposure),
                "gain": int(gain),
                "target": dict(target) if target is not None else None,
                "fits": fits,
            }
            self.frames.append(record)
            return {k: v for k, v in record.items() if k != "fits"}

    def download_frames(self, dest_dir, kind="sub", *, since=None, timeout=None):
        """The facade's download: write ``<id>.fits`` + ``<id>.json`` into
        ``dest_dir`` for the frames of ``kind`` taken at/after ``since``."""
        with self._lock:
            if self.download_error is not None:
                raise self.download_error
            since_epoch = None if since is None else float(since)
            records = [
                r
                for r in self.frames
                if r["kind"] == kind and (since_epoch is None or r["epoch"] >= since_epoch)
            ]
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        downloaded: list[DownloadedFrame] = []
        for record in records:
            meta = FrameMeta(
                id=record["id"],
                kind=record["kind"],
                time=record["time"],
                epoch=record["epoch"],
                exposure=record["exposure"],
                gain=record["gain"],
                target=record["target"],
            )
            fits_path = dest / f"{record['id']}.fits"
            sidecar_path = dest / f"{record['id']}.json"
            fits_path.write_bytes(record["fits"])
            atomic_write_json(sidecar_path, meta_to_sidecar(meta, size=len(record["fits"])))
            downloaded.append(DownloadedFrame(path=fits_path, sidecar=sidecar_path, meta=meta))
        return downloaded


class BareDevice:
    """A device without ``download_frames`` (capture must degrade gracefully)."""

    def add_stacking_progress_handler(self, handler):
        pass

    def remove_stacking_progress_handler(self, handler):
        pass


class FakePreviewStream:
    """Duck-typed preview stream (never started successfully is fine)."""

    def __init__(self):
        self.callbacks: list = []
        self.running = False

    def add_callback(self, callback):
        self.callbacks.append(callback)

    def start(self, tele=True, wide=False):
        self.running = True

    def stop(self):
        self.running = False


# --- SessionLog unit tests ------------------------------------------------------------


def test_session_log_append_persists_jsonl_and_notifies(tmp_path):
    path = tmp_path / "subs" / "S1" / SESSION_LOG_FILE
    log = SessionLog(path)
    seen: list[LogEvent] = []
    log.add_listener(seen.append)

    event = log.append(KIND_GOTO, "goto RA 5.5000h Dec -5.0000°", source=LOG_SOURCE_EXECUTOR, state="slewing", ts=123.5)
    assert event == LogEvent(
        ts=123.5, kind=KIND_GOTO, detail="goto RA 5.5000h Dec -5.0000°",
        source=LOG_SOURCE_EXECUTOR, state="slewing",
    )
    assert seen == [event]
    assert len(log) == 1
    assert log.events() == (event,)
    # persisted as JSONL, atomically, one line per event
    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert LogEvent.from_dict(json.loads(lines[0])) == event
    # a second append rewrites the whole file
    log.append(KIND_FRAME, "stored sub frame sub-0001", frame_id="sub-0001")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [LogEvent.from_dict(json.loads(line)) for line in lines] == list(log.events())
    # frozen
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.kind = "x"  # type: ignore[misc]
    # invalid kind rejected
    with pytest.raises(ValueError):
        log.append("nope", "bad kind")


def test_session_log_listener_failure_is_isolated(tmp_path):
    log = SessionLog(tmp_path / "log.jsonl")

    def bad(event):
        raise RuntimeError("listener bug")

    log.add_listener(bad)
    seen: list[LogEvent] = []
    log.add_listener(seen.append)
    event = log.append(KIND_CAPTURE, "capture started")
    assert seen == [event]  # the bad listener did not break the other one
    log.remove_listener(bad)
    assert log.listeners == (seen.append,)


def test_session_log_load_roundtrip_and_skips_corrupt_lines(tmp_path):
    path = tmp_path / "log.jsonl"
    first = LogEvent(ts=1.0, kind=KIND_SESSION, detail="session S opened", source=LOG_SOURCE_EXECUTOR, state="idle")
    second = LogEvent(ts=2.0, kind=KIND_FRAME, detail="stored sub frame sub-0001", frame_id="sub-0001")
    path.write_text(
        json.dumps(first.to_dict()) + "\nnot json at all\n" + json.dumps(second.to_dict()) + "\n",
        encoding="utf-8",
    )
    log = SessionLog(path)
    assert log.load() == 2
    assert log.events() == (first, second)
    # appending after a load persists everything (old + new)
    log.append(KIND_CAPTURE, "capture started")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    # a missing file loads as empty
    assert SessionLog(tmp_path / "missing.jsonl").load() == 0


def test_classify_executor_event():
    assert classify_executor_event("goto RA 5.5000h Dec -5.0000°") == KIND_GOTO
    assert classify_executor_event("start_stack exposure=10s gain=50 mosaic=False") == KIND_STACK
    assert classify_executor_event("entry 0 done: 2 sub-frames stacked") == KIND_STACK
    assert classify_executor_event("stack finished on entry 1") == KIND_STACK
    assert classify_executor_event("error on entry 0: RpcError: boom") == KIND_ERROR
    assert classify_executor_event("session open failed: SessionOpenError: x") == KIND_ERROR
    assert classify_executor_event("connection lost: timeout") == KIND_ERROR
    assert classify_executor_event("session 20261120T220000000000Z opened") == KIND_SESSION
    assert classify_executor_event("session 20261120T220000000000Z closed") == KIND_SESSION
    assert classify_executor_event("resolving 'M 42'") == KIND_EXECUTOR
    assert classify_executor_event("parked at end of run") == KIND_EXECUTOR
    assert set(LOG_KINDS) == {
        KIND_GOTO, KIND_STACK, KIND_ERROR, KIND_FRAME, KIND_SESSION, KIND_CAPTURE, KIND_EXECUTOR,
    }


def test_session_log_path_lives_in_the_session_directory(project):
    session = start_session(project)
    assert session_log_path(project, session) == project.path / "subs" / session / SESSION_LOG_FILE
    close_session(project, session)


def test_capture_constructor_validation(project):
    session = start_session(project)
    with pytest.raises(ValueError):
        SessionCapture(FakeDevice(), project, "")
    with pytest.raises(ValueError):
        SessionCapture(FakeDevice(), project, session, poll_interval_s=0)
    close_session(project, session)


# --- end-to-end against the real Device + mock Scope -----------------------------------


def test_mock_run_frames_land_in_session_and_logged(device, project):
    """The demoable path: a 2-target night against the mock Scope — the
    mock's frames are downloaded into the session and appear in the log."""
    executor = NightExecutor(device, two_target_queue(), project, frames_per_target=2)
    worker = executor.start()
    assert worker is not None
    session = executor.session
    assert session is not None
    capture = SessionCapture(device, project, session, executor=executor, poll_interval_s=0.05)
    # poll on the test thread (it owns the project db) while the night runs
    deadline = time.monotonic() + 15.0
    while (
        executor.state not in (ExecutorState.DONE, ExecutorState.ABORTED)
        and time.monotonic() < deadline
    ):
        capture.poll_once()
        time.sleep(0.02)
    assert executor.wait(timeout=15.0)
    assert capture.poll_once() >= 0  # final cycle catches the last Stack

    subs = list_frames(project, session, kind="sub")
    stacks = list_frames(project, session, kind="stack")
    assert len(subs) >= 4  # 2 per target (the mock may record one extra tick)
    assert len(stacks) == 2  # one Stack per target
    for frame in subs + stacks:
        assert get_frame_path(project, frame).exists()  # file in the session dir
    assert {f.exposure for f in subs} == {10.0}  # metadata rows from the sidecar
    assert {f.gain for f in subs} == {50}
    assert all(f.session_id == session for f in subs + stacks)

    events = capture.events()
    kinds = [event.kind for event in events]
    assert KIND_GOTO in kinds and KIND_STACK in kinds and KIND_FRAME in kinds
    assert KIND_SESSION in kinds  # session opened/closed, mirrored from the executor
    assert all(event.ts > 0 for event in events)
    assert any(
        event.source == LOG_SOURCE_EXECUTOR and event.kind == KIND_GOTO for event in events
    )
    assert any(
        event.source == LOG_SOURCE_EXECUTOR and event.kind == KIND_SESSION for event in events
    )
    frame_events = [event for event in events if event.kind == KIND_FRAME]
    assert len(frame_events) == len(subs) + len(stacks)
    assert {event.frame_id for event in frame_events} == capture.stored_frame_ids

    # the JSONL log lives in the session directory, one line per event
    log_path = session_log_path(project, session)
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(events)
    assert [LogEvent.from_dict(json.loads(line)) for line in lines] == list(events)

    # dedupe: re-polling stores nothing new
    assert capture.poll_once() == 0
    assert len(list_frames(project, session)) == len(subs) + len(stacks)


# --- worker loop + dedupe + failure handling (FakeDevice) -------------------------------


def test_worker_loop_downloads_and_stores_via_invoke_on_main(project):
    device = FakeDevice()
    session = start_session(project)
    pending: list = []
    capture = SessionCapture(
        device, project, session, poll_interval_s=0.02, invoke_on_main=lambda cb: pending.append(cb)
    )
    worker = capture.start()
    assert worker is not None and capture.running
    assert capture._on_stack_progress in device._progress_handlers  # armed on Stack events

    device.add_frame("sub", exposure=5.0, gain=60)
    assert drain_until(pending, lambda: len(list_frames(project, session, kind="sub")) == 1)
    device.add_frame("stack")
    assert drain_until(pending, lambda: len(list_frames(project, session, kind="stack")) == 1)

    capture.stop()
    assert not capture.running
    assert capture._on_stack_progress not in device._progress_handlers  # disarmed
    frames = list_frames(project, session)
    assert len(frames) == 2
    assert frames[0].exposure == 5.0 and frames[0].gain == 60
    frame_events = [e for e in capture.events() if e.kind == KIND_FRAME]
    assert {e.frame_id for e in frame_events} == {"sub-0001", "stack-0002"}
    assert any(e.kind == KIND_CAPTURE and "capture started" in e.detail for e in capture.events())
    assert any(e.kind == KIND_CAPTURE and "capture stopped" in e.detail for e in capture.events())
    # dedupe: a manual re-poll stores nothing
    assert capture.poll_once() == 0
    assert len(list_frames(project, session)) == 2
    close_session(project, session)


def test_stack_progress_event_wakes_the_worker_promptly(project):
    device = FakeDevice()
    session = start_session(project)
    pending: list = []
    capture = SessionCapture(
        device, project, session, poll_interval_s=60.0, invoke_on_main=lambda cb: pending.append(cb)
    )
    capture.start()
    device.add_frame("sub")
    device.emit_progress(frames=1)  # Stack event -> immediate poll, not after 60s
    assert drain_until(pending, lambda: len(list_frames(project, session, kind="sub")) == 1, timeout=3.0)
    capture.stop()
    close_session(project, session)


def test_download_failure_is_logged_notified_and_recoverable(project):
    device = FakeDevice()
    session = start_session(project)
    bus = NotificationBus()
    capture = SessionCapture(device, project, session, bus=bus)
    device.download_error = RuntimeError("link down")
    assert capture.poll_once() == 0  # never raises
    errors = [e for e in capture.events() if e.kind == KIND_ERROR]
    assert len(errors) == 1 and "link down" in errors[0].detail
    notes = [n for n in bus.log() if n.source == CAPTURE_SOURCE]
    assert len(notes) == 1
    assert notes[0].severity == NotificationSeverity.ERROR
    assert "Frame download failed" in notes[0].title
    # recovers once the link is back
    device.download_error = None
    device.add_frame("sub")
    assert capture.poll_once() == 1
    assert len(list_frames(project, session, kind="sub")) == 1
    close_session(project, session)


def test_capture_restart_dedupes_via_the_jsonl_log(project):
    device = FakeDevice()
    session = start_session(project)
    first = SessionCapture(device, project, session)
    device.add_frame("sub")
    device.add_frame("stack")
    assert first.poll_once() == 2
    assert len(list_frames(project, session)) == 2
    # a new capture in the same session re-reads the log -> no double store
    second = SessionCapture(device, project, session)
    assert second.stored_frame_ids == first.stored_frame_ids == {"sub-0001", "stack-0002"}
    assert second.poll_once() == 0
    assert len(list_frames(project, session)) == 2
    close_session(project, session)


def test_device_without_download_frames_degrades_gracefully(project):
    session = start_session(project)
    bus = NotificationBus()
    capture = SessionCapture(BareDevice(), project, session, bus=bus)
    capture.start()
    assert capture.poll_once() == 0
    errors = [e for e in capture.events() if e.kind == KIND_ERROR]
    assert len(errors) == 1 and "download_frames" in errors[0].detail
    notes = [n for n in bus.log() if n.source == CAPTURE_SOURCE]
    assert len(notes) == 1 and notes[0].severity == NotificationSeverity.WARNING
    capture.stop()
    # still exactly one error event (not one per poll)
    assert len([e for e in capture.events() if e.kind == KIND_ERROR]) == 1
    close_session(project, session)


def test_capture_log_listeners_receive_events(project):
    device = FakeDevice()
    session = start_session(project)
    capture = SessionCapture(device, project, session)
    seen: list[LogEvent] = []
    capture.add_listener(seen.append)
    device.add_frame("sub")
    capture.poll_once()
    assert [e.kind for e in seen] == [KIND_FRAME]
    assert seen[0].frame_id == "sub-0001"
    capture.remove_listener(seen.append)
    assert capture.listeners == ()
    close_session(project, session)


# --- ObserveController integration -------------------------------------------------------


def build_observe_harness(tmp_path, *, invoke_on_main=None):
    """The app-level controllers with fakes — headless, no network, no gi
    (same shape as tests/test_observing_observe.py's harness, plus a
    FakeDevice that can download frames)."""
    state = AppState(tmp_path / "settings.json")
    state.set_setting("scope_ip", "10.9.8.7")
    settings = SettingsController(state)
    device = FakeDevice()
    connection = ConnectionController(settings, device_factory=lambda ip, key_path: device)
    bus = NotificationBus()
    queue_controller = PlanQueueController()
    controller = ObserveController(
        connection,
        queue_controller,
        bus,
        preview_stream_factory=lambda device, host: FakePreviewStream(),
        invoke_on_main=invoke_on_main,
    )
    connection.connect()
    assert connection.wait(2.0)
    return SimpleNamespace(
        tmp_path=tmp_path,
        device=device,
        connection=connection,
        bus=bus,
        queue_controller=queue_controller,
        controller=controller,
    )


def test_observe_controller_starts_and_stops_the_capture(tmp_path):
    pending: list = []
    harness = build_observe_harness(tmp_path, invoke_on_main=lambda cb: pending.append(cb))
    harness.device.auto_frames = True
    harness.queue_controller.add_entry(coord_entry(5.5, -5.0))
    project = create_project("Capture Night", root=tmp_path / "projects")

    harness.controller.start_night(project, frames_per_target=3)
    capture = harness.controller.capture
    assert capture is not None and capture.running
    session = capture.session_id
    assert session is not None

    # drain the marshalled callbacks on the test thread while the night runs
    assert drain_until(
        pending,
        lambda: harness.controller.executor.state in (ExecutorState.DONE, ExecutorState.ABORTED),
        timeout=15.0,
    )
    assert drain_until(pending, lambda: not capture.running, timeout=5.0)
    assert not capture.running  # DONE stopped the capture

    frames = list_frames(project, session)
    assert len(frames) >= 3  # 3 sub-frames + the final Stack
    assert len(list_frames(project, session, kind="stack")) == 1
    for frame in frames:
        assert get_frame_path(project, frame).exists()
    events = capture.events()
    kinds = {event.kind for event in events}
    assert {KIND_GOTO, KIND_STACK, KIND_FRAME, KIND_SESSION} <= kinds
    assert any(event.source == LOG_SOURCE_EXECUTOR for event in events)
    assert session_log_path(project, session).exists()
    lines = session_log_path(project, session).read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(events)
    close_session(project, session)
    project.close()


def test_observe_controller_capture_factory_failure_warns_but_night_runs(tmp_path):
    pending: list = []
    harness = build_observe_harness(tmp_path, invoke_on_main=lambda cb: pending.append(cb))
    harness.device.auto_frames = True
    harness.queue_controller.add_entry(coord_entry(5.5, -5.0))

    def broken_factory(*args, **kwargs):
        raise RuntimeError("no capture for you")

    harness.controller._capture_factory = broken_factory
    project = create_project("Capture Night", root=tmp_path / "projects")
    harness.controller.start_night(project, frames_per_target=1)
    assert harness.controller.capture is None  # capture unavailable, night runs
    assert drain_until(
        pending,
        lambda: harness.controller.executor.state in (ExecutorState.DONE, ExecutorState.ABORTED),
        timeout=15.0,
    )
    notes = [n for n in harness.bus.log() if n.source == OBSERVE_SOURCE]
    assert any(n.severity == NotificationSeverity.WARNING and "capture" in n.title.lower() for n in notes)
    assert any(n.severity == NotificationSeverity.INFO and n.title == "Night started" for n in notes)
    project.close()


def test_default_capture_factory_binds_the_executor(project):
    device = FakeDevice()
    session = start_session(project)
    executor = NightExecutor(device, two_target_queue(), project)
    capture = default_capture_factory(device, project, session, executor)
    assert isinstance(capture, SessionCapture)
    assert capture.executor is executor
    assert capture.session_id == session
    assert capture.project is project
    assert capture.device is device
    close_session(project, session)


# --- package boundary + exports ------------------------------------------------------------


def test_capture_module_imports_no_gi_and_package_stays_lazy():
    code = (
        "import sys;"
        "import seestar_controller.observing as o;"
        "assert 'seestar_controller.observing.capture' not in sys.modules;"
        "assert 'seestar_controller.link' not in sys.modules;"
        "assert 'gi' not in sys.modules;"
        "from seestar_controller.observing import SessionCapture, SessionLog, LogEvent;"
        "assert 'seestar_controller.observing.capture' in sys.modules;"
        "assert 'seestar_controller.link.frames' in sys.modules;"
        "assert 'gi' not in sys.modules;"
        "print('clean')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_capture_names_are_lazy_exports():
    for name in (
        "CAPTURE_SOURCE",
        "KIND_CAPTURE",
        "KIND_ERROR",
        "KIND_EXECUTOR",
        "KIND_FRAME",
        "KIND_GOTO",
        "KIND_SESSION",
        "KIND_STACK",
        "LOG_KINDS",
        "LOG_SOURCE_CAPTURE",
        "LOG_SOURCE_EXECUTOR",
        "LOG_SOURCES",
        "SESSION_LOG_FILE",
        "LogEvent",
        "LogListener",
        "SessionCapture",
        "SessionLog",
        "classify_executor_event",
        "session_log_path",
        "CaptureFactory",
        "default_capture_factory",
        "SessionLogDialog",
    ):
        assert name in observing.__all__, name
    assert observing._EXECUTOR_EXPORTS["SessionCapture"] == "capture"
    assert observing._EXECUTOR_EXPORTS["SessionLog"] == "capture"
    assert observing._EXECUTOR_EXPORTS["LogEvent"] == "capture"
    assert observing._EXECUTOR_EXPORTS["default_capture_factory"] == "observe_controller"
    assert observing._EXECUTOR_EXPORTS["CaptureFactory"] == "observe_controller"
    assert observing._WIDGET_EXPORTS["SessionLogDialog"] == "session_log_dialog"
    assert observing.SessionCapture is SessionCapture
    assert observing.SessionLog is SessionLog
    assert observing.LogEvent is LogEvent
    assert observing.classify_executor_event is classify_executor_event
    assert observing.session_log_path is session_log_path
