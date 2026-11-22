"""Observing ticket 07 — the frame-stored event (headless, pure layer).

:class:`~seestar_controller.observing.capture.SessionCapture` fires a
:class:`~seestar_controller.observing.capture.FrameStoredEvent` to its
frame-stored listeners on every frame it stores into the session from
:meth:`~seestar_controller.observing.capture.SessionCapture._apply` (not on a
dedupe re-poll, not when the store fails, a failing listener never breaks the
others), and the
:class:`~seestar_controller.observing.observe_controller.ObserveController`
forwards it to its own frame-stored listeners **marshalled via
``invoke_on_main``** (the app binds ``GLib.idle_add``) and detaches the
forwarding when the capture stops. The Projects view listens on the
controller (widget tests: tests/test_observing_frame_stored_widgets.py) —
this file covers the pure chain end to end: store a frame during a session
and the event arrives, without any project reopen.

Device double: the :class:`FakeDevice` from tests/test_observing_capture.py
(scripted ``download_frames`` writing real FITS). No network, no gi.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from test_observing_capture import (
    FakeDevice,
    build_observe_harness,
    drain_until,
)

from seestar_controller import observing
from seestar_controller.observing import (
    FrameStoredEvent,
    ObserveController,
    SessionCapture,
)
from seestar_controller.observing import capture as capture_module
from seestar_controller.store import (
    KIND_STACK,
    KIND_SUB,
    close_session,
    create_project,
    list_frames,
    start_session,
)
from seestar_controller.store.frames import store_frame as real_store_frame
from seestar_controller.ui.notifications import NotificationBus


@pytest.fixture()
def live_project(tmp_path):
    """A tmp project with one open session, closed at the end."""
    project = create_project("Live Night", root=tmp_path / "projects")
    session = start_session(project)
    yield project, session
    close_session(project, session)
    project.close()


def _capture(device, project, session) -> SessionCapture:
    return SessionCapture(device, project, session, bus=NotificationBus())


# --- SessionCapture fires the event ------------------------------------------------------


def test_capture_fires_frame_stored_event_per_stored_frame(live_project):
    project, session = live_project
    device = FakeDevice()
    capture = _capture(device, project, session)
    events: list[FrameStoredEvent] = []
    capture.add_frame_listener(events.append)

    device.add_frame(KIND_SUB, target={"name": "M 42"})
    device.add_frame(KIND_SUB)
    device.add_frame(KIND_STACK)
    assert capture.poll_once() == 3

    assert len(events) == 3
    sub_events = [e for e in events if e.kind == KIND_SUB]
    stack_events = [e for e in events if e.kind == KIND_STACK]
    assert [e.frame_id for e in sub_events] == ["sub-0001", "sub-0002"]
    assert [e.frame_id for e in stack_events] == ["stack-0003"]
    for event in events:
        assert event.session_id == session
        # the path is the project-relative stored FITS path of the frame
        assert (Path(project.path) / event.path).exists()
    stored_paths = {f.path for f in list_frames(project, session)}
    assert {e.path for e in events} == stored_paths


def test_capture_does_not_fire_again_on_a_dedupe_repoll(live_project):
    project, session = live_project
    device = FakeDevice()
    capture = _capture(device, project, session)
    events: list[FrameStoredEvent] = []
    capture.add_frame_listener(events.append)

    device.add_frame(KIND_SUB)
    assert capture.poll_once() == 1
    assert len(events) == 1
    # nothing new downloaded -> nothing stored -> no new event
    assert capture.poll_once() == 0
    assert capture.poll_once() == 0
    assert len(events) == 1


def test_capture_does_not_fire_when_the_store_fails(live_project, monkeypatch):
    """A frame whose ``store_frame`` raises is not "stored" — no event (the
    watermark stays, so the frame is retried and fires once it stores)."""
    project, session = live_project
    device = FakeDevice()
    capture = _capture(device, project, session)
    events: list[FrameStoredEvent] = []
    capture.add_frame_listener(events.append)

    failing = {"on": True}

    def flaky_store(project, session_id, kind, fits_bytes, meta=None):
        if failing["on"]:
            raise OSError("disk full")
        return real_store_frame(project, session_id, kind, fits_bytes, meta=meta)

    monkeypatch.setattr("seestar_controller.observing.capture.store_frame", flaky_store)

    device.add_frame(KIND_SUB)
    assert capture.poll_once() == 0
    assert events == []
    assert len(list_frames(project, session)) == 0

    failing["on"] = False
    assert capture.poll_once() == 1  # retried, now stored
    assert len(events) == 1
    assert events[0].frame_id == "sub-0001"
    assert len(list_frames(project, session)) == 1


def test_frame_stored_listener_failure_is_isolated(live_project):
    """A raising listener is skipped; the others still get the event and the
    poll still stores the frame."""
    project, session = live_project
    device = FakeDevice()
    capture = _capture(device, project, session)
    events: list[FrameStoredEvent] = []

    def bad_listener(event):
        raise RuntimeError("boom")

    capture.add_frame_listener(bad_listener)
    capture.add_frame_listener(events.append)
    device.add_frame(KIND_SUB)
    assert capture.poll_once() == 1
    assert len(events) == 1
    assert len(list_frames(project, session, kind=KIND_SUB)) == 1


def test_frame_listener_registration_is_idempotent(live_project):
    project, session = live_project
    device = FakeDevice()
    capture = _capture(device, project, session)
    received: list[FrameStoredEvent] = []

    listener = received.append
    assert capture.add_frame_listener(listener) is listener  # decorator form returns it
    capture.add_frame_listener(listener)  # idempotent
    assert capture.frame_listeners == (listener,)

    capture.remove_frame_listener(listener)
    capture.remove_frame_listener(listener)  # no-op when not registered
    assert capture.frame_listeners == ()

    device.add_frame(KIND_SUB)
    assert capture.poll_once() == 1
    assert received == []


# --- ObserveController forwards the event via invoke_on_main -----------------------------


def _slow_capture_factory(device, project, session_id, executor, **kwargs):
    """A capture whose worker never polls on its own (tests drive
    ``poll_once`` by hand)."""
    kwargs["poll_interval_s"] = 60.0
    return SessionCapture(device, project, session_id, executor=executor, **kwargs)


def test_observe_controller_forwards_frame_stored_marshalled_via_invoke_on_main(tmp_path):
    pending: list = []
    harness = build_observe_harness(tmp_path, invoke_on_main=lambda cb: pending.append(cb))
    harness.controller._capture_factory = _slow_capture_factory
    project = create_project("Live Night", root=tmp_path / "projects")
    session = start_session(project)
    controller = harness.controller
    assert isinstance(controller, ObserveController)

    received: list[FrameStoredEvent] = []
    controller.add_frame_listener(received.append)
    controller._start_capture(harness.device, project, session, None)
    capture = controller.capture
    assert capture is not None
    assert capture.frame_listeners == (controller._on_capture_frame_stored,)

    # a frame stored during the session arrives at the controller listener
    # ONLY after the queued invoke_on_main callback runs (main-loop safe)
    harness.device.add_frame(KIND_SUB)
    assert capture.poll_once() == 1
    assert received == []
    assert drain_until(pending, lambda: len(received) == 1)
    assert received[0] == FrameStoredEvent(
        session_id=session, kind=KIND_SUB, frame_id="sub-0001", path=received[0].path
    )
    assert (Path(project.path) / received[0].path).exists()

    # stopping the capture detaches the forwarding: no more events
    controller._stop_capture()
    assert capture.frame_listeners == ()
    harness.device.add_frame(KIND_SUB)
    assert capture.poll_once() == 1
    assert drain_until(pending, lambda: True)  # drain whatever is queued
    assert len(received) == 1

    close_session(project, session)
    project.close()


def test_observe_controller_frame_listener_failure_is_isolated(tmp_path):
    pending: list = []
    harness = build_observe_harness(tmp_path, invoke_on_main=lambda cb: pending.append(cb))
    harness.controller._capture_factory = _slow_capture_factory
    project = create_project("Live Night", root=tmp_path / "projects")
    session = start_session(project)
    controller = harness.controller

    received: list[FrameStoredEvent] = []

    def bad_listener(event):
        raise RuntimeError("boom")

    controller.add_frame_listener(bad_listener)
    controller.add_frame_listener(received.append)
    controller._start_capture(harness.device, project, session, None)
    capture = controller.capture
    assert capture is not None

    harness.device.add_frame(KIND_SUB)
    assert capture.poll_once() == 1
    assert drain_until(pending, lambda: len(received) == 1)  # the good listener ran
    controller._stop_capture()
    close_session(project, session)
    project.close()


# --- package boundary + exports ------------------------------------------------------------


def test_frame_stored_names_are_lazy_exports_and_gi_free():
    code = (
        "import sys;"
        "import seestar_controller.observing as o;"
        "assert 'seestar_controller.observing.capture' not in sys.modules;"
        "assert 'gi' not in sys.modules;"
        "from seestar_controller.observing import FrameStoredEvent, FrameStoredListener;"
        "assert 'seestar_controller.observing.capture' in sys.modules;"
        "assert 'gi' not in sys.modules;"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout

    assert "FrameStoredEvent" in observing.__all__
    assert "FrameStoredListener" in observing.__all__
    assert observing._EXECUTOR_EXPORTS["FrameStoredEvent"] == "capture"
    assert observing._EXECUTOR_EXPORTS["FrameStoredListener"] == "capture"
    assert observing.FrameStoredEvent is capture_module.FrameStoredEvent
    assert observing.FrameStoredListener is capture_module.FrameStoredListener
