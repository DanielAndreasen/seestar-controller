"""Stacking control against the mock Scope: start -> progress events -> stop."""

import threading
import time

import pytest

from seestar_controller.link.device import (
    STACK_EVENT,
    START_STACK_METHOD,
    STOP_STACK_METHOD,
    Device,
    StackingProgress,
)
from seestar_controller.link.transport import RpcError


@pytest.fixture()
def stacking_device(transport, mock_scope):
    """A Device facade on the connected transport fixture, fast stacking events."""
    mock_scope.slew_duration = 0.05
    mock_scope.stack_event_interval = 0.02
    d = Device.connect("127.0.0.1", transport=transport, goto_timeout=2.0)
    yield d
    try:
        if d.connected and d.state().mode == "stacking":
            d.stop_stack()
    except Exception:
        pass
    d.disconnect()


def test_start_stack_begins_and_mock_records_params(stacking_device, mock_scope):
    result = stacking_device.start_stack(10.0, 80)
    # start_stack returns after stacking has begun
    assert result.mode == "stacking"
    assert result.stacking_active
    assert result.stacking.state == "working"
    assert result.stacking.frames >= 0
    assert result.stacking.lapse_ms == result.stacking.frames * mock_scope.subframe_ms
    # the mock recorded the params exactly as sent on the wire
    assert mock_scope.last_stack_params == {"exp_ms": 10000, "gain": 80, "is_mosaic": False}
    assert mock_scope.scope_state()["mode"] == "stacking"
    assert stacking_device.state().mode == "stacking"


def test_start_stack_with_mosaic_framing(stacking_device, mock_scope):
    result = stacking_device.start_stack(20.0, 60, {"pan": 2.0, "tilt": 1.5})
    assert result.mode == "stacking"
    assert mock_scope.last_stack_params == {
        "exp_ms": 20000,
        "gain": 60,
        "is_mosaic": True,
        "mosaic_pan": 2.0,
        "mosaic_tilt": 1.5,
    }


def test_start_stack_mosaic_tuple_and_bool(stacking_device, mock_scope):
    stacking_device.start_stack(30.0, 100, (4.0, 0.0))
    assert mock_scope.last_stack_params == {
        "exp_ms": 30000,
        "gain": 100,
        "is_mosaic": True,
        "mosaic_pan": 4.0,
        "mosaic_tilt": 0.0,
    }
    stacking_device.stop_stack()
    stacking_device.start_stack(10.0, 80, True)
    assert mock_scope.last_stack_params == {"exp_ms": 10000, "gain": 80, "is_mosaic": True}


def test_progress_events_arrive_with_increasing_frames(stacking_device, mock_scope):
    progress: list[StackingProgress] = []
    enough = threading.Event()

    def on_progress(p: StackingProgress) -> None:
        progress.append(p)
        if p.frames >= 3:
            enough.set()

    stacking_device.add_stacking_progress_handler(on_progress)
    raw_events: list = []
    stacking_device.add_event_handler(STACK_EVENT, raw_events.append)

    stacking_device.start_stack(10.0, 80)
    assert enough.wait(timeout=3), f"only {len(progress)} progress events arrived"

    frames = [p.frames for p in progress]
    assert frames == sorted(frames) and len(set(frames)) == len(frames), frames
    assert all(p.state == "working" for p in progress)
    lapses = [p.lapse_ms for p in progress]
    assert lapses == sorted(lapses) and len(set(lapses)) == len(lapses), lapses
    # each stacked sub-frame adds one 10 s lapse (GLOSSARY: sub-frame = 10 s)
    assert progress[-1].lapse_ms == progress[-1].frames * mock_scope.subframe_ms
    # the raw wire events are Stack events carrying the same payload
    assert raw_events and all(e.name == STACK_EVENT for e in raw_events)
    assert raw_events[-1].payload["frames"] == progress[-1].frames
    # state() reflects the same progress while stacking
    state = stacking_device.state()
    assert state.mode == "stacking"
    assert state.stacking.frames >= 3
    assert state.stacking.lapse_ms == state.stacking.frames * mock_scope.subframe_ms


def test_stop_stack_ends_cleanly_with_final_event(stacking_device, mock_scope):
    progress: list[StackingProgress] = []
    final = threading.Event()

    def on_progress(p: StackingProgress) -> None:
        progress.append(p)
        if p.state == "idle":
            final.set()

    stacking_device.add_stacking_progress_handler(on_progress)
    stacking_device.start_stack(10.0, 80)
    time.sleep(0.15)  # let a few sub-frames stack
    result = stacking_device.stop_stack()

    assert result.mode == "idle"
    assert not result.stacking_active
    assert result.stacking.state == "idle"
    assert result.stacking.frames >= 1
    assert final.wait(timeout=3), "no final Stack event pushed on stop"
    last = progress[-1]
    assert last.state == "idle"
    assert last.frames == result.stacking.frames
    assert last.lapse_ms == result.stacking.lapse_ms
    assert mock_scope.scope_state()["mode"] == "idle"
    assert stacking_device.state().stacking.state == "idle"
    # no further progress events after the stop
    count = len(progress)
    time.sleep(0.1)
    assert len(progress) == count


def test_start_stack_twice_is_rejected(stacking_device, mock_scope):
    stacking_device.start_stack(10.0, 80)
    with pytest.raises(RpcError):
        stacking_device.start_stack(10.0, 80)
    assert mock_scope.scope_state()["mode"] == "stacking"
    # the first stack is still the recorded one
    assert mock_scope.last_stack_params == {"exp_ms": 10000, "gain": 80, "is_mosaic": False}


def test_stop_stack_when_not_stacking_raises(stacking_device):
    with pytest.raises(RpcError):
        stacking_device.stop_stack()
    assert stacking_device.state().mode == "idle"


def test_start_stack_invalid_params_rejected_client_side(stacking_device, mock_scope):
    bad = [
        (0.5, 80, False),          # exposure below range
        (60.5, 80, False),         # exposure above range
        (10.0, -1, False),         # gain below range
        (10.0, 101, False),        # gain above range
        (10.0, 80, {"pan": 11.0, "tilt": 0.0}),   # mosaic pan out of range
        (10.0, 80, {"pan": -1.0, "tilt": 0.0}),   # mosaic pan negative
        (10.0, 80, (1.0, 2.0, 3.0)),              # bad framing shape
        (10.0, 80, "wide"),                       # bad mosaic type
        ("ten", 80, False),                       # bad exposure type
        (10.0, "high", False),                    # bad gain type
    ]
    for exposure, gain, mosaic in bad:
        with pytest.raises(ValueError):
            stacking_device.start_stack(exposure, gain, mosaic)
    # nothing was sent to the scope
    assert mock_scope.last_stack_params is None
    assert stacking_device.state().mode == "idle"


def test_mock_rejects_invalid_stack_params_with_nonzero_code(transport, mock_scope):
    """Protocol level: the mock itself validates and replies with code != 0."""
    bad_params = [
        {"exp_ms": 500, "gain": 80},       # exposure 0.5 s below range
        {"exp_ms": 61000, "gain": 80},     # exposure 61 s above range
        {"exp_ms": 10000, "gain": 200},    # gain above range
        {"exp_ms": 10000, "gain": -5},     # gain below range
        {"gain": 80},                      # missing exp_ms
        {"exp_ms": 10000},                 # missing gain
        {"exp_ms": "abc", "gain": 80},     # non-numeric exp_ms
        {"exp_ms": 10000, "gain": 80, "is_mosaic": "yes"},
        {"exp_ms": 10000, "gain": 80, "is_mosaic": True, "mosaic_pan": 99.0},
    ]
    for params in bad_params:
        with pytest.raises(RpcError) as excinfo:
            transport.request(START_STACK_METHOD, params)
        assert excinfo.value.reply.code != 0
    with pytest.raises(RpcError) as excinfo:
        transport.request(STOP_STACK_METHOD)  # not stacking
    assert excinfo.value.reply.code != 0
    assert mock_scope.last_stack_params is None
    assert mock_scope.scope_state()["mode"] == "idle"


def test_mock_start_stacking_python_api_validates_and_records(mock_scope):
    """MockScope.start_stacking/stop_stacking as direct test hooks."""
    with pytest.raises(ValueError):
        mock_scope.start_stacking(0.2, 80)
    with pytest.raises(ValueError):
        mock_scope.start_stacking(10.0, 500)
    assert mock_scope.scope_state()["mode"] == "idle"

    params = mock_scope.start_stacking(10.0, 80, True)
    assert params == {"exp_ms": 10000, "gain": 80, "is_mosaic": True}
    assert mock_scope.last_stack_params == params
    assert mock_scope.scope_state()["mode"] == "stacking"
    assert mock_scope.scope_state()["stacking"] == {
        "state": "working",
        "lapse_ms": 0,
        "frames": 0,
    }
    with pytest.raises(ValueError):
        mock_scope.start_stacking(10.0, 80)  # already stacking

    result = mock_scope.stop_stacking()
    assert result["mode"] == "idle"
    assert result["state"] == "idle"
    assert result["frames"] == mock_scope.scope_state()["stacking"]["frames"]
    assert mock_scope.scope_state()["mode"] == "idle"
    with pytest.raises(ValueError):
        mock_scope.stop_stacking()  # not stacking anymore


def test_on_stacking_progress_decorator_receives_typed_progress(stacking_device, mock_scope):
    received: list[StackingProgress] = []
    done = threading.Event()

    @stacking_device.on_stacking_progress
    def on_progress(p: StackingProgress) -> None:
        received.append(p)
        if p.frames >= 2:
            done.set()

    stacking_device.start_stack(10.0, 80)
    assert done.wait(timeout=3)
    assert all(isinstance(p, StackingProgress) for p in received)
    assert [p.frames for p in received] == sorted(p.frames for p in received)


def test_remove_stacking_progress_handler_stops_delivery(stacking_device, mock_scope):
    received: list[StackingProgress] = []
    handler = lambda p: received.append(p)  # noqa: E731
    stacking_device.add_stacking_progress_handler(handler)
    stacking_device.remove_stacking_progress_handler(handler)
    stacking_device.start_stack(10.0, 80)
    time.sleep(0.1)
    stacking_device.stop_stack()
    assert received == []


def test_stacking_progress_visible_in_state_after_events(stacking_device, mock_scope):
    """state().stacking tracks the pushed progress while the Stack grows."""
    seen = threading.Event()

    def on_progress(p: StackingProgress) -> None:
        if p.frames >= 2:
            seen.set()

    stacking_device.add_stacking_progress_handler(on_progress)
    stacking_device.start_stack(10.0, 80)
    assert seen.wait(timeout=3)
    state = stacking_device.state()
    assert state.mode == "stacking"
    assert state.stacking.state == "working"
    assert state.stacking.frames >= 2
    assert state.stacking.lapse_ms > 0
