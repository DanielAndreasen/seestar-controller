"""Pure-Python connection-controller tests — no gi, no real network.

Covers the ticket-03 pure layer ``seestar_controller.ui.connection``:
``ConnectionController`` driven by a FAKE ``device_factory`` (success ->
``connected`` with the device + scope state; failure -> ``error`` with the
message; disconnect; lifecycle events ``connection_lost`` / ``reconnecting``
/ ``reconnected``; listener notifications; ``invoke_on_main`` marshalling;
invalid/empty IP -> error without calling the factory; double connect
rejected), ``ConnectionStatus`` / ``ConnectionSnapshot`` /
``ConnectionStateError``, ``describe_scope_state``, the package exports, and
a subprocess module-boundary test (no ``gi``, no link import, widget layer
stays lazy).

The last section is integration-ish: the REAL
:class:`~seestar_controller.link.device.Device` against the mock Scope
(``link.mock_scope``) on 127.0.0.1 — connect -> ``connected`` with the scope
state, then ``mock_scope.drop_connections()`` -> ``reconnecting`` ->
``connected`` again via the resilience reconnect, and a refused port ->
``error``.
"""

import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from seestar_controller import ui
from seestar_controller.ui.connection import (
    CONNECTION_LOST_EVENT,
    LIFECYCLE_EVENTS,
    RECONNECTED_EVENT,
    RECONNECTING_EVENT,
    ConnectionController,
    ConnectionSnapshot,
    ConnectionStateError,
    ConnectionStatus,
    describe_scope_state,
)
from seestar_controller.ui.settings import SettingsController
from seestar_controller.ui.state import AppState, UIStateError

# --- fakes ---------------------------------------------------------------------


class FakeScopeState:
    """A duck-typed stand-in for the link's DeviceState."""

    def __init__(self, mode="idle", target=None):
        self.mode = mode
        self.target = target


class FakeDevice:
    """A fake device facade: state()/disconnect()/event handlers + fire()."""

    def __init__(self, state=None):
        self._state = state if state is not None else FakeScopeState()
        self.disconnected = False
        self.handlers: dict[str, list] = {}

    def state(self):
        return self._state

    def disconnect(self):
        self.disconnected = True

    def add_event_handler(self, name, handler):
        self.handlers.setdefault(name, []).append(handler)

    def remove_event_handler(self, name, handler):
        if handler in self.handlers.get(name, []):
            self.handlers[name].remove(handler)

    def fire(self, name, payload=None):
        """Dispatch a lifecycle event to the registered handlers."""
        event = SimpleNamespace(name=name, payload=payload or {})
        for handler in list(self.handlers.get(name, [])):
            handler(event)


def make_factory(device=None, error=None, calls=None, gate=None):
    """A device_factory recording its (ip, key_path) calls.

    ``gate`` (a ``threading.Event``) blocks the factory until set — used to
    keep the worker thread deterministically in-flight while asserting the
    intermediate CONNECTING state.
    """

    def factory(ip, key_path):
        if calls is not None:
            calls.append((ip, key_path))
        if gate is not None:
            gate.wait(5)
        if error is not None:
            raise error
        return device

    return factory


def wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# --- fixtures --------------------------------------------------------------------


@pytest.fixture()
def settings(tmp_path):
    """A SettingsController over a tmp AppState with a scope IP + key path."""
    state = AppState(tmp_path / "settings.json")
    state.set_setting("scope_ip", "192.168.1.50")
    state.set_setting("key_path", str(tmp_path / "key.pem"))
    return SettingsController(state)


@pytest.fixture()
def controller(settings):
    """A ConnectionController over the tmp settings (default direct invoke)."""
    return ConnectionController(settings)


# --- initial state + snapshot ------------------------------------------------------


def test_initial_state_is_disconnected(controller):
    assert controller.status == ConnectionStatus.DISCONNECTED
    assert controller.device is None
    assert controller.scope_state is None
    assert controller.error is None
    assert controller.scope_ip == ""
    assert controller.listeners == ()
    snapshot = controller.snapshot
    assert isinstance(snapshot, ConnectionSnapshot)
    assert snapshot.status == ConnectionStatus.DISCONNECTED
    assert snapshot.scope_state is None and snapshot.error is None
    assert controller.settings is not None


def test_status_enum_values():
    assert {s.value for s in ConnectionStatus} == {
        "disconnected",
        "connecting",
        "connected",
        "reconnecting",
        "error",
    }
    assert str(ConnectionStatus.CONNECTED) == "connected"
    assert LIFECYCLE_EVENTS == (CONNECTION_LOST_EVENT, RECONNECTING_EVENT, RECONNECTED_EVENT)


# --- connect: success ---------------------------------------------------------------


def test_connect_success_goes_connecting_then_connected(settings):
    device = FakeDevice(FakeScopeState(mode="idle", target=(5.5, 12.25)))
    calls = []
    gate = threading.Event()
    c = ConnectionController(
        settings, device_factory=make_factory(device, calls=calls, gate=gate)
    )
    seen = []
    c.add_listener(seen.append)

    worker = c.connect()
    assert worker is not None and isinstance(worker, threading.Thread)
    assert c.status == ConnectionStatus.CONNECTING  # synchronous transition
    assert seen[-1].status == ConnectionStatus.CONNECTING  # notified already
    gate.set()  # let the worker finish
    assert c.wait(5)

    assert c.status == ConnectionStatus.CONNECTED
    assert c.device is device
    assert c.scope_state is device.state()
    assert c.scope_state.mode == "idle" and c.scope_state.target == (5.5, 12.25)
    assert c.error is None
    assert c.scope_ip == "192.168.1.50"
    # the factory got the scope IP + key path from the settings
    assert calls == [("192.168.1.50", settings.values["key_path"])]
    # listeners were notified for both transitions, in order
    assert [s.status for s in seen] == [
        ConnectionStatus.CONNECTING,
        ConnectionStatus.CONNECTED,
    ]
    assert seen[-1].scope_state is c.scope_state
    assert seen[-1].scope_ip == "192.168.1.50"
    # lifecycle handlers armed on the device
    assert set(device.handlers) == set(LIFECYCLE_EVENTS)


def test_connect_accepts_explicit_scope_ip_and_factory_override(settings):
    device = FakeDevice()
    calls = []
    c = ConnectionController(settings, device_factory=make_factory(error=RuntimeError("no")))
    c.connect("10.0.0.9", device_factory=make_factory(device, calls=calls))
    assert c.wait(5)
    assert c.status == ConnectionStatus.CONNECTED
    assert calls == [("10.0.0.9", settings.values["key_path"])]


def test_connect_reads_settings_at_call_time(tmp_path):
    state = AppState(tmp_path / "settings.json")
    sc = SettingsController(state)
    c = ConnectionController(sc, device_factory=make_factory(FakeDevice()))
    sc.update(scope_ip="10.1.2.3", project_root=str(tmp_path))
    c.connect()
    assert c.wait(5)
    assert c.scope_ip == "10.1.2.3"


def test_controller_accepts_a_plain_app_state(tmp_path):
    state = AppState(tmp_path / "settings.json")
    state.set_setting("scope_ip", "seestar.local")
    calls = []
    c = ConnectionController(state, device_factory=make_factory(FakeDevice(), calls=calls))
    c.connect()
    assert c.wait(5)
    assert c.status == ConnectionStatus.CONNECTED
    assert calls == [("seestar.local", state.get_setting("key_path"))]


# --- connect: failure -> error -------------------------------------------------------


def test_connect_failure_sets_error_with_message(settings):
    c = ConnectionController(
        settings, device_factory=make_factory(error=OSError("connection refused"))
    )
    seen = []
    c.add_listener(seen.append)
    worker = c.connect()
    assert worker is not None and c.wait(5)

    assert c.status == ConnectionStatus.ERROR
    assert c.device is None and c.scope_state is None
    assert "OSError" in c.error and "connection refused" in c.error  # ty: ignore[unsupported-operator]
    assert [s.status for s in seen] == [ConnectionStatus.CONNECTING, ConnectionStatus.ERROR]
    assert seen[-1].error == c.error


def test_connect_failure_when_state_read_fails(settings):
    class BadStateDevice(FakeDevice):
        def state(self):
            raise RuntimeError("scope busy")

    c = ConnectionController(settings, device_factory=make_factory(BadStateDevice()))
    c.connect()
    assert c.wait(5)
    assert c.status == ConnectionStatus.ERROR
    assert "scope busy" in c.error  # ty: ignore[unsupported-operator]


# --- connect: invalid / empty IP -> error WITHOUT calling the factory -------------------


def test_empty_scope_ip_is_error_without_calling_factory(tmp_path):
    state = AppState(tmp_path / "settings.json")  # default scope_ip is ""
    calls = []
    c = ConnectionController(
        SettingsController(state), device_factory=make_factory(FakeDevice(), calls=calls)
    )
    seen = []
    c.add_listener(seen.append)
    assert c.connect() is None  # no worker thread
    assert c.status == ConnectionStatus.ERROR
    assert "not set" in c.error  # ty: ignore[unsupported-operator]
    assert calls == []
    assert [s.status for s in seen] == [ConnectionStatus.ERROR]


def test_invalid_scope_ip_is_error_without_calling_factory(settings):
    calls = []
    c = ConnectionController(
        settings, device_factory=make_factory(FakeDevice(), calls=calls)
    )
    assert c.connect("999.1.1.1") is None
    assert c.status == ConnectionStatus.ERROR
    assert "scope_ip" in c.error  # ty: ignore[unsupported-operator]
    assert calls == []
    # another invalid form behaves the same
    c2 = ConnectionController(
        settings, device_factory=make_factory(FakeDevice(), calls=calls)
    )
    assert c2.connect("bad ip!") is None
    assert c2.status == ConnectionStatus.ERROR
    assert calls == []


# --- double connect ---------------------------------------------------------------------


def test_double_connect_is_rejected(settings):
    device = FakeDevice()
    calls = []
    release = threading.Event()

    def slow_factory(ip, key_path):
        calls.append((ip, key_path))
        release.wait(5)
        return device

    c = ConnectionController(settings, device_factory=slow_factory)
    worker = c.connect()
    assert c.status == ConnectionStatus.CONNECTING
    with pytest.raises(ConnectionStateError) as excinfo:
        c.connect()
    assert "connecting" in str(excinfo.value)
    assert isinstance(excinfo.value, UIStateError) and isinstance(excinfo.value, RuntimeError)
    release.set()
    worker.join(5)  # ty: ignore[missing-union-attribute]
    assert c.wait(5)
    assert c.status == ConnectionStatus.CONNECTED
    # still connected -> rejected again, factory not called twice
    with pytest.raises(ConnectionStateError):
        c.connect()
    assert len(calls) == 1
    # after disconnect, connect works again
    c.disconnect()
    worker2 = c.connect()
    assert worker2 is not None and c.wait(5)
    assert c.status == ConnectionStatus.CONNECTED
    assert len(calls) == 2


def test_connect_after_error_retries(settings):
    c = ConnectionController(
        settings, device_factory=make_factory(error=OSError("refused"))
    )
    c.connect()
    assert c.wait(5)
    assert c.status == ConnectionStatus.ERROR
    # from error, connect is allowed again (retry)
    worker = c.connect(device_factory=make_factory(FakeDevice()))
    assert worker is not None and c.wait(5)
    assert c.status == ConnectionStatus.CONNECTED


# --- disconnect ---------------------------------------------------------------------------


def test_disconnect_from_connected(settings):
    device = FakeDevice()
    c = ConnectionController(settings, device_factory=make_factory(device))
    c.connect()
    assert c.wait(5)
    seen = []
    c.add_listener(seen.append)

    c.disconnect()
    assert c.status == ConnectionStatus.DISCONNECTED
    assert c.device is None and c.scope_state is None and c.error is None
    assert device.disconnected
    assert [s.status for s in seen] == [ConnectionStatus.DISCONNECTED]
    # lifecycle handlers disarmed
    assert all(not handlers for handlers in device.handlers.values())


def test_disconnect_when_already_disconnected_is_a_noop(controller):
    seen = []
    controller.add_listener(seen.append)
    controller.disconnect()
    assert controller.status == ConnectionStatus.DISCONNECTED
    assert seen == []


def test_disconnect_clears_error_state(settings):
    c = ConnectionController(
        settings, device_factory=make_factory(error=OSError("boom"))
    )
    c.connect()
    assert c.wait(5)
    assert c.status == ConnectionStatus.ERROR
    seen = []
    c.add_listener(seen.append)
    c.disconnect()
    assert c.status == ConnectionStatus.DISCONNECTED
    assert c.error is None
    assert [s.status for s in seen] == [ConnectionStatus.DISCONNECTED]


def test_disconnect_while_connecting_discards_the_late_result(settings):
    device = FakeDevice()
    started = threading.Event()
    release = threading.Event()

    def slow_factory(ip, key_path):
        started.set()
        release.wait(5)
        return device

    c = ConnectionController(settings, device_factory=slow_factory)
    worker = c.connect()
    assert started.wait(2)
    c.disconnect()
    assert c.status == ConnectionStatus.DISCONNECTED
    release.set()
    worker.join(5)  # ty: ignore[missing-union-attribute]
    assert c.wait(5)
    # the late success must NOT flip the state back to connected ...
    assert c.status == ConnectionStatus.DISCONNECTED
    assert c.device is None
    # ... and the stale device is disconnected
    assert device.disconnected


# --- lifecycle events -----------------------------------------------------------------------


def test_lifecycle_events_update_state_and_notify(settings):
    device = FakeDevice(FakeScopeState(mode="idle"))
    c = ConnectionController(settings, device_factory=make_factory(device))
    c.connect()
    assert c.wait(5)
    seen = []
    c.add_listener(seen.append)

    device.fire(CONNECTION_LOST_EVENT, {"reason": "socket eof"})
    assert c.status == ConnectionStatus.RECONNECTING
    device.fire(RECONNECTING_EVENT, {"reason": "socket eof", "max_attempts": None})
    assert c.status == ConnectionStatus.RECONNECTING
    device.fire(RECONNECTED_EVENT, {"attempts": 1})
    assert c.status == ConnectionStatus.CONNECTED

    assert [s.status for s in seen] == [
        ConnectionStatus.RECONNECTING,
        ConnectionStatus.RECONNECTING,
        ConnectionStatus.CONNECTED,
    ]


def test_reconnected_refreshes_the_scope_state(settings):
    device = FakeDevice(FakeScopeState(mode="idle"))
    c = ConnectionController(settings, device_factory=make_factory(device))
    c.connect()
    assert c.wait(5)
    assert c.scope_state.mode == "idle"  # ty: ignore[missing-union-attribute]
    device._state = FakeScopeState(mode="stacking", target=(1.0, 2.0))
    device.fire(RECONNECTED_EVENT, {"attempts": 2})
    assert c.status == ConnectionStatus.CONNECTED
    assert c.scope_state.mode == "stacking"  # ty: ignore[missing-union-attribute]
    assert c.scope_state.target == (1.0, 2.0)  # ty: ignore[missing-union-attribute]


def test_lifecycle_events_from_a_stale_device_are_ignored(settings):
    device_a = FakeDevice()
    c = ConnectionController(settings, device_factory=make_factory(device_a))
    c.connect()
    assert c.wait(5)
    c.disconnect()
    # handlers were disarmed on disconnect, so firing does nothing at all
    device_a.fire(CONNECTION_LOST_EVENT, {"reason": "socket eof"})
    assert c.status == ConnectionStatus.DISCONNECTED
    # a second connect arms the NEW device only
    device_b = FakeDevice()
    c.connect(device_factory=make_factory(device_b))
    assert c.wait(5)
    device_a.fire(RECONNECTED_EVENT, {"attempts": 1})
    assert c.status == ConnectionStatus.CONNECTED
    device_b.fire(CONNECTION_LOST_EVENT, {"reason": "socket eof"})
    assert c.status == ConnectionStatus.RECONNECTING


# --- invoke_on_main marshalling ---------------------------------------------------------------


def test_worker_results_are_marshalled_via_invoke_on_main(settings):
    device = FakeDevice()
    queue = []
    gate = threading.Event()

    def queued_invoke(callback):
        queue.append(callback)

    c = ConnectionController(
        settings,
        device_factory=make_factory(device, gate=gate),
        invoke_on_main=queued_invoke,
    )
    seen = []
    c.add_listener(seen.append)

    worker = c.connect()
    # the synchronous CONNECTING transition mutated the state, but its
    # listener notification is queued (not run yet); the worker is still
    # blocked in the factory, so only that one callback is queued
    assert c.status == ConnectionStatus.CONNECTING
    assert seen == [] and len(queue) == 1

    gate.set()
    worker.join(5)  # ty: ignore[missing-union-attribute]
    assert c.wait(5)
    # the worker finished, but its result is only marshalled, not applied
    assert c.status == ConnectionStatus.CONNECTING
    assert len(queue) == 2 and seen == []

    # drain the queue (what the GLib main loop would do)
    for callback in queue:
        callback()
    assert c.status == ConnectionStatus.CONNECTED
    assert c.device is device
    assert [s.status for s in seen] == [
        ConnectionStatus.CONNECTING,
        ConnectionStatus.CONNECTED,
    ]


def test_lifecycle_events_are_marshalled_too(settings):
    device = FakeDevice()
    queue = []
    c = ConnectionController(
        settings, device_factory=make_factory(device), invoke_on_main=queue.append
    )
    c.connect()
    assert c.wait(5)
    for callback in queue:
        callback()
    assert c.status == ConnectionStatus.CONNECTED

    device.fire(CONNECTION_LOST_EVENT, {"reason": "socket eof"})
    assert c.status == ConnectionStatus.CONNECTED  # not applied yet
    assert queue  # ... but marshalled
    for callback in queue:
        callback()
    assert c.status == ConnectionStatus.RECONNECTING


def test_default_invoke_on_main_is_a_direct_call(settings):
    c = ConnectionController(settings, device_factory=make_factory(FakeDevice()))
    seen = []
    c.add_listener(seen.append)
    c.connect()
    assert c.wait(5)
    # direct invoke: everything already applied synchronously
    assert c.status == ConnectionStatus.CONNECTED
    assert [s.status for s in seen] == [
        ConnectionStatus.CONNECTING,
        ConnectionStatus.CONNECTED,
    ]


# --- listeners -----------------------------------------------------------------------------


def test_listeners_add_remove_idempotent_decorator(controller):
    calls_a, calls_b = [], []
    controller.add_listener(calls_a.append)
    controller.add_listener(calls_b.append)
    controller.add_listener(calls_a.append)  # idempotent
    assert len(controller.listeners) == 2

    @controller.add_listener
    def on_change(snapshot):
        on_change.seen = snapshot

    assert on_change in controller.listeners
    controller.remove_listener(calls_a.append)
    controller.remove_listener(calls_a.append)  # no-op when absent
    assert len(controller.listeners) == 2


def test_wait_without_worker_returns_true(controller):
    assert controller.wait(0.1) is True


# --- describe_scope_state ---------------------------------------------------------------------


def test_describe_scope_state():
    assert describe_scope_state(None) == "no scope state"
    assert describe_scope_state(FakeScopeState(mode="idle")) == "mode idle, no target"
    text = describe_scope_state(FakeScopeState(mode="stacking", target=(5.5, 12.25)))
    assert "mode stacking" in text and "5.50" in text and "+12.25" in text
    assert describe_scope_state(FakeScopeState(mode="parked", target=("bad",))) == "mode parked"
    assert describe_scope_state(SimpleNamespace(mode="slewing", target=None)) == (
        "mode slewing, no target"
    )


# --- public API + module boundary ---------------------------------------------------------------


def test_public_api_is_exported_from_package():
    for name in (
        "ConnectionController",
        "ConnectionStatus",
        "ConnectionSnapshot",
        "ConnectionStateError",
        "ConnectionListener",
        "DeviceFactory",
        "InvokeOnMain",
        "default_device_factory",
        "describe_scope_state",
        "CONNECTION_LOST_EVENT",
        "RECONNECTING_EVENT",
        "RECONNECTED_EVENT",
        "LIFECYCLE_EVENTS",
    ):
        assert name in ui.__all__, name
        assert hasattr(ui, name), name
    assert "ConnectionPanel" in ui.__all__ and "ConnectionPanel" in ui._WIDGET_EXPORTS
    assert ui.ConnectionController is ConnectionController
    assert ui.ConnectionStatus is ConnectionStatus
    assert ui.describe_scope_state is describe_scope_state
    assert issubclass(ui.ConnectionStateError, ui.UIStateError)
    assert callable(ui.default_device_factory)


def test_pure_connection_module_imports_no_gi_no_link_and_widget_layer_stays_lazy():
    code = (
        "import sys;"
        "import seestar_controller.ui;"
        "import seestar_controller.ui.connection;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.ui.app' not in sys.modules, 'app imported eagerly';"
        "assert 'seestar_controller.ui.views' not in sys.modules, 'views imported eagerly';"
        "assert 'seestar_controller.ui.settings_dialog' not in sys.modules, "
        "'settings_dialog imported eagerly';"
        "assert 'seestar_controller.ui.connection_panel' not in sys.modules, "
        "'connection_panel imported eagerly';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


# --- integration: the REAL Device against the mock Scope on 127.0.0.1 -------------------------


def mock_device_factory(mock_scope, keypair):
    """A device_factory using the real Device facade against the mock Scope."""
    from seestar_controller.link.device import Device
    from seestar_controller.link.resilience import ReconnectPolicy

    private_pem, _public_pem = keypair

    def factory(ip, key_path):
        return Device.connect(
            ip,
            mock_scope.command_port,
            pem=private_pem,
            heartbeat_interval=0.1,
            heartbeat_timeout=0.5,
            request_timeout=2.0,
            reconnect=ReconnectPolicy(base_delay=0.05, cap=0.2),
        )

    return factory


def _localhost_settings(tmp_path):
    state = AppState(tmp_path / "settings.json")
    state.set_setting("scope_ip", "127.0.0.1")
    return SettingsController(state)


def test_real_device_against_mock_scope_connect_state_and_reconnect(
    mock_scope, keypair, tmp_path
):
    from seestar_controller.link.device import Device

    c = ConnectionController(
        _localhost_settings(tmp_path), device_factory=mock_device_factory(mock_scope, keypair)
    )
    seen = []
    c.add_listener(seen.append)

    worker = c.connect()
    assert worker is not None and c.wait(10)
    assert c.status == ConnectionStatus.CONNECTED
    assert isinstance(c.device, Device)
    # the successful connect shows the scope's current state
    assert c.scope_state is not None
    assert c.scope_state.mode == "idle"
    assert c.scope_state.target is None
    assert c.error is None

    # drop the link: resilience reconnects -> reconnecting -> connected again
    mock_scope.drop_connections()
    assert wait_until(lambda: c.status == ConnectionStatus.RECONNECTING, timeout=5), (
        "connection_lost did not surface as reconnecting"
    )
    assert wait_until(lambda: c.status == ConnectionStatus.CONNECTED, timeout=10), (
        "reconnected did not surface as connected"
    )
    assert c.scope_state is not None  # refreshed after the reconnect
    statuses = [s.status for s in seen]
    assert statuses[0] == ConnectionStatus.CONNECTING
    assert ConnectionStatus.RECONNECTING in statuses
    assert statuses[-1] == ConnectionStatus.CONNECTED

    c.disconnect()
    assert c.status == ConnectionStatus.DISCONNECTED
    assert c.device is None


def test_real_device_connect_refused_goes_to_error(mock_scope, keypair, tmp_path):
    from seestar_controller.link.device import Device

    # a port with nothing listening on it
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()

    def factory(ip, key_path):
        return Device.connect(
            ip,
            dead_port,
            pem=keypair[0],
            connect_timeout=1.0,
            request_timeout=1.0,
            reconnect=None,
        )

    c = ConnectionController(_localhost_settings(tmp_path), device_factory=factory)
    seen = []
    c.add_listener(seen.append)
    worker = c.connect()
    assert worker is not None and c.wait(10)
    assert c.status == ConnectionStatus.ERROR
    assert c.error and "ConnectionRefusedError" in c.error
    assert c.device is None
    assert [s.status for s in seen] == [ConnectionStatus.CONNECTING, ConnectionStatus.ERROR]
