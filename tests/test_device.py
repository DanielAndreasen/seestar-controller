"""Device facade against the mock Scope: connect -> state -> goto -> park."""

import threading
import time

import pytest

from seestar_controller.link.device import (
    GOTO_EVENT,
    PARK_EVENT,
    Device,
    DeviceState,
    GotoTimeoutError,
    StackingProgress,
    connect,
)
from seestar_controller.link.transport import RpcError


@pytest.fixture()
def device(transport, mock_scope):
    """A Device facade on the connected transport fixture, fast mock slew."""
    mock_scope.slew_duration = 0.05
    d = Device.connect("127.0.0.1", transport=transport, goto_timeout=2.0)
    yield d
    d.disconnect()


def test_connect_returns_facade_with_live_state(device):
    assert device.connected
    assert device.transport.verified
    state = device.state()
    assert isinstance(state, DeviceState)
    assert state.mode == "idle"
    assert state.target is None
    assert state.ra is None and state.dec is None
    assert state.stacking == StackingProgress(state="idle", lapse_ms=0, frames=0)
    assert not state.slewing and not state.parked
    # connect() primed the live state
    assert device.last_state == state


def test_connect_creates_own_transport(mock_scope, keypair):
    """connect(ip) builds and connects its own LinkTransport (signed handshake)."""
    private_pem, _public_pem = keypair
    d = connect(
        "127.0.0.1",
        mock_scope.command_port,
        pem=private_pem,
        heartbeat_interval=0.1,
        request_timeout=2.0,
    )
    try:
        assert d.connected
        assert d.transport.verified
        assert d.state().mode == "idle"
        assert mock_scope.client_count == 1
    finally:
        d.disconnect()
    assert not d.connected


def test_connect_rejects_transport_plus_kwargs(transport):
    with pytest.raises(ValueError):
        Device.connect("127.0.0.1", transport=transport, pem=b"nope")


def test_state_reflects_mode_target_and_stacking_progress(device, mock_scope):
    mock_scope.set_stacking_progress("working", lapse_ms=42000, frames=4)
    device.goto(3.25, -15.5)
    state = device.state()
    assert state.mode == "idle"
    assert state.target == (3.25, -15.5)
    assert state.ra == 3.25
    assert state.dec == -15.5
    assert state.stacking.state == "working"
    assert state.stacking.lapse_ms == 42000
    assert state.stacking.frames == 4
    # the slew really went over the wire with these params
    assert mock_scope.last_goto == {"ra": 3.25, "dec": -15.5}


def test_goto_reports_arrival_via_event(device, mock_scope):
    events: list = []
    arrived = threading.Event()

    def on_goto(event):
        events.append(event)
        if event.payload.get("state") == "complete":
            arrived.set()

    device.add_event_handler(GOTO_EVENT, on_goto)
    result = device.goto(10.0, 20.0)
    assert arrived.wait(timeout=3), "no ScopeGoto complete event"
    states = [e.payload.get("state") for e in events]
    assert "slewing" in states and "complete" in states
    complete = next(e for e in events if e.payload.get("state") == "complete")
    assert complete.payload["ra"] == 10.0
    assert complete.payload["dec"] == 20.0
    # goto returns the state after arrival
    assert result.mode == "idle"
    assert result.target == (10.0, 20.0)
    assert device.state().target == (10.0, 20.0)


def test_goto_arrival_also_visible_by_state_polling(device, mock_scope):
    """While the slew runs, state() polling shows mode "slewing"."""
    mock_scope.slew_duration = 0.4
    holder: dict = {}
    slew_thread = threading.Thread(
        target=lambda: holder.setdefault("result", device.goto(1.0, 2.0))
    )
    slew_thread.start()
    try:
        saw_slewing = False
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if device.state().slewing:
                saw_slewing = True
                break
            time.sleep(0.01)
        assert saw_slewing, "state polling never saw mode=slewing"
    finally:
        slew_thread.join(timeout=5)
    assert not slew_thread.is_alive()
    assert holder["result"].mode == "idle"
    assert holder["result"].target == (1.0, 2.0)


def test_goto_invalid_coordinates_rejected_client_side(device, mock_scope):
    for ra, dec in [(-0.1, 0.0), (24.0, 0.0), (0.0, -90.1), (0.0, 90.1)]:
        with pytest.raises(ValueError):
            device.goto(ra, dec)
    # nothing was sent to the scope
    assert mock_scope.last_goto is None


def test_goto_timeout_raises_and_link_stays_up(device, mock_scope):
    mock_scope.slew_duration = 1.0
    with pytest.raises(GotoTimeoutError) as excinfo:
        device.goto(7.0, 8.0, timeout=0.2)
    assert isinstance(excinfo.value, TimeoutError)
    assert device.connected  # a goto timeout is not a connection loss
    # the slew still completes afterwards and state polling sees the arrival
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        state = device.state()
        if not state.slewing:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("slew never completed")
    assert state.target == (7.0, 8.0)


def test_park_sets_mode_parked(device, mock_scope):
    park_events: list = []
    parked = threading.Event()

    def on_park(event):
        park_events.append(event)
        parked.set()

    device.add_event_handler(PARK_EVENT, on_park)
    result = device.park()
    assert result.mode == "parked"
    assert result.parked
    assert parked.wait(timeout=3), "no ScopePark event pushed"
    assert park_events[0].payload["state"] == "parked"
    # live state and the mock's own state agree
    assert device.state().mode == "parked"
    assert mock_scope.scope_state()["mode"] == "parked"


def test_facade_surfaces_pushed_events_through_callbacks(device, mock_scope):
    received: list = []
    done = threading.Event()

    device.add_event_handler("Stack", lambda e: (received.append(e), done.set()))
    mock_scope.push_event({"Event": "Stack", "state": "working", "lapse_ms": 10})
    assert done.wait(timeout=3)
    assert received[0].name == "Stack"
    assert received[0].payload["lapse_ms"] == 10


def test_wildcard_handler_sees_goto_events(device):
    names: list[str] = []
    device.add_event_handler("*", lambda e: names.append(e.name))
    device.goto(2.0, 3.0)
    assert "ScopeGoto" in names


def test_remove_event_handler_stops_delivery(device, mock_scope):
    received: list = []
    handler = lambda e: received.append(e)
    device.add_event_handler("Stack", handler)
    device.remove_event_handler("Stack", handler)
    mock_scope.push_event({"Event": "Stack"})
    time.sleep(0.3)
    assert received == []


def test_on_event_alias_registers_handler(device, mock_scope):
    received: list = []
    done = threading.Event()

    @device.on_event("Test")
    def handler(event):
        received.append(event)
        done.set()

    mock_scope.push_event({"Event": "Test", "value": 42})
    assert done.wait(timeout=3)
    assert received[0].payload["value"] == 42


def test_context_manager_disconnects(mock_scope, keypair):
    private_pem, _public_pem = keypair
    with connect(
        "127.0.0.1",
        mock_scope.command_port,
        pem=private_pem,
        heartbeat_interval=0.1,
        request_timeout=2.0,
    ) as d:
        assert d.connected
        assert d.state().mode == "idle"
    assert not d.connected


def test_mock_rejects_out_of_range_goto_at_protocol_level(transport):
    """The mock itself also validates ra/dec (non-zero reply code -> RpcError)."""
    with pytest.raises(RpcError):
        transport.request("scope_goto_target", {"ra": 99.0, "dec": 0.0})
    with pytest.raises(RpcError):
        transport.request("scope_goto_target", {"ra": "abc", "dec": 0.0})
