"""Pure-Python notification tests — no gi, no real network (except the mock).

Covers the ticket-04 pure layer ``seestar_controller.ui.notifications``:

- :class:`NotificationSeverity` / :class:`Notification` (frozen, defaults,
  severity coercion, validation);
- :class:`NotificationBus` — push notifies listeners, ``log()`` newest-first,
  the bounded ring-buffer capacity drops the oldest, ``clear()``, listener
  add/remove (idempotent, decorator), consecutive-duplicate dedup
  (A, A -> one entry; A, B, A -> three), thread-safety of concurrent pushes,
  a raising listener is logged and skipped (``push`` still returns ``True``
  and the remaining listeners still run);
- :class:`NotificationController` driven by a real
  :class:`~seestar_controller.ui.connection.ConnectionController` with a FAKE
  ``device_factory``: connect failure -> one ERROR "Connection error"
  notification; initial connect -> NO notification; ``connection_lost`` /
  ``reconnecting`` -> one WARNING "Link lost" (deduped); ``reconnected`` ->
  INFO "Reconnected"; device events via the injectable ``subscribe``
  (``Stack`` progress -> INFO, final ``Stack`` idle -> skipped,
  ``stack_finished`` -> INFO, ``scope_error`` -> ERROR); the default
  ``subscribe`` arms the facade's ``add_event_handler``; ``invoke_on_main``
  marshalling of device events; ``detach()``; the bus is the only channel
  (no widget/dialog API on the controller);
- package exports + a subprocess module-boundary test (no ``gi``, no link
  import, widget layer stays lazy).

The last section is integration-ish: the REAL
:class:`~seestar_controller.link.device.Device` against the mock Scope
(``link.mock_scope``) on 127.0.0.1 — connect (silent), then
``mock_scope.drop_connections()`` -> the bus log contains the WARNING
"Link lost" (reconnecting) and then the INFO "Reconnected" notifications; a
``mock_fail`` RPC -> an ERROR "Scope error" notification; a stack run ->
INFO "Stacking" + "Stack finished" notifications.
"""

import logging
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from seestar_controller import ui
from seestar_controller.ui.connection import (
    CONNECTION_LOST_EVENT,
    RECONNECTED_EVENT,
    RECONNECTING_EVENT,
    ConnectionController,
    ConnectionStatus,
)
from seestar_controller.ui.notifications import (
    CONNECTION_SOURCE,
    DEFAULT_LOG_CAPACITY,
    DEVICE_EVENTS,
    SCOPE_ERROR_EVENT,
    STACK_EVENT,
    STACK_FINISHED_EVENT,
    Notification,
    NotificationBus,
    NotificationController,
    NotificationSeverity,
    default_event_subscribe,
)
from seestar_controller.ui.settings import SettingsController
from seestar_controller.ui.state import AppState

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
        """Dispatch a device event to the registered handlers."""
        event = SimpleNamespace(name=name, payload=payload or {})
        for handler in list(self.handlers.get(name, [])):
            handler(event)


def make_factory(device=None, error=None, calls=None):
    """A device_factory recording its (ip, key_path) calls."""

    def factory(ip, key_path):
        if calls is not None:
            calls.append((ip, key_path))
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
def bus():
    return NotificationBus()


@pytest.fixture()
def connection(settings):
    """A ConnectionController over the tmp settings (default direct invoke)."""
    return ConnectionController(settings)


@pytest.fixture()
def controller(bus, connection):
    """A NotificationController over the bus + connection (direct invoke)."""
    return NotificationController(bus, connection)


# --- NotificationSeverity / Notification -------------------------------------------


def test_severity_enum_values():
    assert {s.value for s in NotificationSeverity} == {"info", "warning", "error"}
    assert str(NotificationSeverity.ERROR) == "error"
    assert NotificationSeverity("warning") is NotificationSeverity.WARNING


def test_notification_defaults_and_coercion():
    before = datetime.now(UTC)
    n = Notification(NotificationSeverity.INFO, "Title", "msg", "src")
    after = datetime.now(UTC)
    assert n.timestamp.tzinfo is not None
    assert before <= n.timestamp <= after
    # a plain string severity is coerced
    n2 = Notification("error", "T", "", "src")  # ty: ignore[invalid-argument-type]
    assert n2.severity is NotificationSeverity.ERROR
    # a naive timestamp is assumed UTC
    naive = Notification("info", "T", "", "src", datetime(2026, 1, 1, 12, 0, 0))  # ty: ignore[invalid-argument-type]  # noqa: DTZ001
    assert naive.timestamp.tzinfo == UTC
    # invalid severity / fields rejected
    with pytest.raises(ValueError):
        Notification("bogus", "T", "", "src")  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError):
        Notification(NotificationSeverity.INFO, 42, "", "src")  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError):
        Notification(NotificationSeverity.INFO, "T", "", "src", "not-a-datetime")  # ty: ignore[invalid-argument-type]


def test_notification_is_frozen():
    n = Notification(NotificationSeverity.INFO, "T", "m", "s")
    with pytest.raises(Exception):  # noqa: B017
        n.title = "other"  # type: ignore[misc]  # ty: ignore[invalid-assignment]


# --- NotificationBus: push / log / capacity / clear ---------------------------------


def test_push_notifies_listeners_and_logs_newest_first(bus):
    seen = []
    bus.add_listener(seen.append)
    n1 = Notification(NotificationSeverity.INFO, "one", "m1", "a")
    n2 = Notification(NotificationSeverity.ERROR, "two", "m2", "b")
    assert bus.push(n1) is True
    assert bus.push(n2) is True
    assert seen == [n1, n2]
    assert bus.log() == (n2, n1)  # newest first
    assert len(bus) == 2
    assert bus.capacity == DEFAULT_LOG_CAPACITY == 200


def test_push_rejects_non_notifications(bus):
    with pytest.raises(ValueError):
        bus.push("not a notification")


def test_log_capacity_is_a_bounded_ring_buffer():
    bus = NotificationBus(capacity=3)
    notes = [
        Notification(NotificationSeverity.INFO, f"n{i}", "", "s") for i in range(5)
    ]
    for n in notes:
        assert bus.push(n) is True
    assert len(bus) == 3
    # the two oldest dropped off; newest first
    assert [n.title for n in bus.log()] == ["n4", "n3", "n2"]


def test_bus_capacity_validation():
    with pytest.raises(ValueError):
        NotificationBus(capacity=0)
    with pytest.raises(ValueError):
        NotificationBus(capacity=-1)
    with pytest.raises(ValueError):
        NotificationBus(capacity=True)
    assert NotificationBus(capacity=1).capacity == 1


def test_clear_empties_the_log_silently(bus):
    seen = []
    bus.add_listener(seen.append)
    bus.push(Notification(NotificationSeverity.INFO, "t", "m", "s"))
    assert len(bus) == 1
    bus.clear()
    assert bus.log() == () and len(bus) == 0
    assert len(seen) == 1  # clear does NOT notify


def test_listeners_add_remove_idempotent_decorator(bus):
    calls_a, calls_b = [], []
    bus.add_listener(calls_a.append)
    bus.add_listener(calls_b.append)
    bus.add_listener(calls_a.append)  # idempotent
    assert len(bus.listeners) == 2

    @bus.add_listener
    def on_notification(n):
        on_notification.seen = n

    assert on_notification in bus.listeners
    bus.remove_listener(calls_a.append)
    bus.remove_listener(calls_a.append)  # no-op when absent
    assert len(bus.listeners) == 2
    bus.push(Notification(NotificationSeverity.INFO, "t", "", "s"))
    assert len(calls_b) == 1 and hasattr(on_notification, "seen")


def test_consecutive_identical_pushes_are_deduped(bus):
    seen = []
    bus.add_listener(seen.append)
    n1 = Notification(NotificationSeverity.WARNING, "Link lost", "reconnecting", CONNECTION_SOURCE)
    n2 = Notification(NotificationSeverity.WARNING, "Link lost", "reconnecting", CONNECTION_SOURCE)
    assert n1 is not n2 and n1.timestamp != n2.timestamp  # timestamps differ
    assert bus.push(n1) is True
    assert bus.push(n2) is False  # consecutive duplicate dropped
    assert len(bus) == 1 and seen == [n1]
    # a different notification in between re-enables it (A, B, A kept)
    other = Notification(NotificationSeverity.INFO, "other", "", "s")
    assert bus.push(other) is True
    assert bus.push(n2) is True
    assert len(bus) == 3 and len(seen) == 3
    assert [n.title for n in bus.log()] == ["Link lost", "other", "Link lost"]


def test_dedup_ignores_only_consecutive_duplicates_not_the_whole_log(bus):
    a = Notification(NotificationSeverity.ERROR, "Scope error", "m", SCOPE_ERROR_EVENT)
    b = Notification(NotificationSeverity.ERROR, "Scope error", "different", SCOPE_ERROR_EVENT)
    assert bus.push(a) is True
    assert bus.push(b) is True  # different message -> not a duplicate
    assert len(bus) == 2


def test_concurrent_pushes_are_thread_safe():
    bus = NotificationBus(capacity=500)
    seen = []
    lock = threading.Lock()

    def listener(n):
        with lock:
            seen.append(n)

    bus.add_listener(listener)

    def worker(i):
        for j in range(10):
            bus.push(Notification(NotificationSeverity.INFO, f"t{i}-{j}", "", "s"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(bus) == 100
    assert len(seen) == 100
    assert len(bus.log()) == 100


def test_raising_listener_is_logged_and_does_not_break_push(bus, caplog):
    def boom(n):
        raise RuntimeError("listener exploded")

    seen = []
    bus.add_listener(boom)  # registered BEFORE the recording listener
    bus.add_listener(seen.append)
    n = Notification(NotificationSeverity.ERROR, "Scope error", "m", SCOPE_ERROR_EVENT)
    with caplog.at_level(logging.ERROR, logger="seestar_controller.ui.notifications"):
        assert bus.push(n) is True  # push still returns True
    assert seen == [n]  # the recording listener still received it
    assert bus.log() == (n,) and len(bus) == 1  # the entry is stored
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "listener" in errors[0].getMessage()
    assert errors[0].exc_info is not None  # logged via logger.exception
    # the bus stays usable afterwards
    n2 = Notification(NotificationSeverity.INFO, "other", "", "s")
    assert bus.push(n2) is True
    assert seen == [n, n2]
    assert [x.title for x in bus.log()] == ["other", "Scope error"]


# --- NotificationController: connection snapshots -------------------------------------


def test_connect_failure_pushes_one_error_notification(bus, settings):
    c = ConnectionController(
        settings, device_factory=make_factory(error=OSError("connection refused"))
    )
    nc = NotificationController(bus, c)
    c.connect()
    assert c.wait(5)
    assert c.status == ConnectionStatus.ERROR
    log = bus.log()
    assert len(log) == 1
    n = log[0]
    assert n.severity is NotificationSeverity.ERROR
    assert n.title == "Connection error"
    assert "connection refused" in n.message
    assert n.source == CONNECTION_SOURCE
    assert nc.bus is bus and nc.connection is c


def test_initial_connect_is_silent(bus, settings):
    device = FakeDevice()
    c = ConnectionController(settings, device_factory=make_factory(device))
    NotificationController(bus, c)
    c.connect()
    assert c.wait(5)
    assert c.status == ConnectionStatus.CONNECTED
    assert bus.log() == ()  # the panel already shows the connected state


def test_invalid_ip_error_also_notifies(bus, tmp_path):
    state = AppState(tmp_path / "settings.json")  # scope_ip unset
    c = ConnectionController(SettingsController(state), device_factory=make_factory(FakeDevice()))
    NotificationController(bus, c)
    assert c.connect() is None
    assert c.status == ConnectionStatus.ERROR
    log = bus.log()
    assert len(log) == 1 and log[0].severity is NotificationSeverity.ERROR
    assert "not set" in log[0].message


def test_link_lost_and_reconnect_translations(bus, settings):
    device = FakeDevice()
    c = ConnectionController(settings, device_factory=make_factory(device))
    NotificationController(bus, c)
    c.connect()
    assert c.wait(5)
    assert bus.log() == ()

    device.fire(CONNECTION_LOST_EVENT, {"reason": "socket eof"})
    assert c.status == ConnectionStatus.RECONNECTING
    device.fire(RECONNECTING_EVENT, {"reason": "socket eof"})
    # both lifecycle events map to the same WARNING -> deduped to ONE
    log = bus.log()
    assert len(log) == 1
    assert log[0].severity is NotificationSeverity.WARNING
    assert log[0].title == "Link lost"
    assert "Reconnecting" in log[0].message
    assert log[0].source == CONNECTION_SOURCE

    device.fire(RECONNECTED_EVENT, {"attempts": 1})
    assert c.status == ConnectionStatus.CONNECTED
    log = bus.log()
    assert len(log) == 2
    assert log[0].severity is NotificationSeverity.INFO  # newest first
    assert log[0].title == "Reconnected"
    assert "192.168.1.50" in log[0].message
    assert log[1].title == "Link lost"


def test_reconnect_after_disconnect_is_not_a_reconnected_toast(bus, settings):
    device = FakeDevice()
    c = ConnectionController(settings, device_factory=make_factory(device))
    NotificationController(bus, c)
    c.connect()
    assert c.wait(5)
    c.disconnect()
    assert bus.log() == ()  # disconnect is silent
    c.connect()
    assert c.wait(5)
    assert c.status == ConnectionStatus.CONNECTED
    assert bus.log() == ()  # a fresh connect is not a "Reconnected"


def test_disconnect_unsubscribes_device_events(bus, settings):
    device = FakeDevice()
    c = ConnectionController(settings, device_factory=make_factory(device))
    nc = NotificationController(bus, c)
    c.connect()
    assert c.wait(5)
    assert nc.device is device
    assert set(DEVICE_EVENTS) <= set(device.handlers)
    c.disconnect()
    assert nc.device is None
    assert all(not device.handlers.get(name) for name in DEVICE_EVENTS)


# --- NotificationController: device events ---------------------------------------------


def test_device_events_translate_to_notifications(bus, settings):
    device = FakeDevice()
    c = ConnectionController(settings, device_factory=make_factory(device))
    nc = NotificationController(bus, c)
    c.connect()
    assert c.wait(5)
    assert nc.device is device

    device.fire(STACK_EVENT, {"state": "working", "lapse_ms": 30000, "frames": 3})
    device.fire(STACK_EVENT, {"state": "idle", "lapse_ms": 40000, "frames": 4})  # skipped
    device.fire(STACK_FINISHED_EVENT, {"state": "idle", "lapse_ms": 40000, "frames": 4})
    device.fire(SCOPE_ERROR_EVENT, {"id": 7, "method": "mock_fail", "code": 500, "result": {"error": "boom"}})

    log = bus.log()  # newest first
    assert [n.title for n in log] == ["Scope error", "Stack finished", "Stacking"]
    stacking, finished, error = log[2], log[1], log[0]
    assert stacking.severity is NotificationSeverity.INFO
    assert stacking.source == STACK_EVENT
    assert "3 sub-frames" in stacking.message and "30000" in stacking.message
    assert finished.severity is NotificationSeverity.INFO
    assert finished.source == STACK_FINISHED_EVENT
    assert "4 sub-frames" in finished.message
    assert error.severity is NotificationSeverity.ERROR
    assert error.source == SCOPE_ERROR_EVENT
    assert "mock_fail" in error.message and "500" in error.message and "boom" in error.message


def test_scope_error_without_result(bus, settings):
    device = FakeDevice()
    c = ConnectionController(settings, device_factory=make_factory(device))
    NotificationController(bus, c)
    c.connect()
    assert c.wait(5)
    device.fire(SCOPE_ERROR_EVENT, {"method": "scope_park", "code": -32602})
    (n,) = bus.log()
    assert n.severity is NotificationSeverity.ERROR
    assert "scope_park" in n.message and "-32602" in n.message


def test_injectable_subscribe_is_used_and_unsubscribed(bus, settings):
    device = FakeDevice()
    c = ConnectionController(settings, device_factory=make_factory(device))
    subscriptions = []  # (device, name, handler)
    unsubscribed = []

    def fake_subscribe(dev, name, handler):
        subscriptions.append((dev, name, handler))
        dev.add_event_handler(name, handler)  # keep the fake device working

        def unsubscribe():
            unsubscribed.append(name)
            dev.remove_event_handler(name, handler)

        return unsubscribe

    nc = NotificationController(bus, c, subscribe=fake_subscribe)
    c.connect()
    assert c.wait(5)
    assert [(d, name) for d, name, _ in subscriptions] == [
        (device, name) for name in DEVICE_EVENTS
    ]
    assert nc.device is device
    # events still flow through the injected subscription
    device.fire(STACK_FINISHED_EVENT, {"frames": 2, "lapse_ms": 20000})
    assert bus.log()[0].title == "Stack finished"
    # disconnect -> all unsubscribers called
    c.disconnect()
    assert sorted(unsubscribed) == sorted(DEVICE_EVENTS)
    assert nc.device is None


def test_default_subscribe_uses_the_facade_registry(bus, settings):
    device = FakeDevice()
    c = ConnectionController(settings, device_factory=make_factory(device))
    NotificationController(bus, c)  # default subscribe
    c.connect()
    assert c.wait(5)
    for name in DEVICE_EVENTS:
        assert device.handlers.get(name), name  # armed via add_event_handler
    # default_event_subscribe returns an unsubscribe closure
    handler = lambda e: None
    unsub = default_event_subscribe(device, "SomeEvent", handler)
    assert device.handlers["SomeEvent"] == [handler]
    unsub()
    assert not device.handlers.get("SomeEvent")


def test_device_events_are_marshalled_via_invoke_on_main(bus, settings):
    device = FakeDevice()
    queue = []
    c = ConnectionController(settings, device_factory=make_factory(device))
    nc = NotificationController(bus, c, invoke_on_main=queue.append)
    c.connect()
    assert c.wait(5)
    for callback in queue:  # drain the connection-controller queue
        callback()
    assert nc.device is device

    device.fire(STACK_EVENT, {"state": "working", "frames": 1, "lapse_ms": 10000})
    assert bus.log() == ()  # marshalled, not applied yet
    assert queue
    for callback in queue:
        callback()
    assert len(bus.log()) == 1 and bus.log()[0].title == "Stacking"


def test_detach_stops_everything(bus, settings):
    device = FakeDevice()
    c = ConnectionController(settings, device_factory=make_factory(device))
    nc = NotificationController(bus, c)
    c.connect()
    assert c.wait(5)
    nc.detach()
    nc.detach()  # idempotent
    assert nc not in c.listeners
    assert nc.device is None
    assert all(not device.handlers.get(name) for name in DEVICE_EVENTS)
    # no more notifications from either channel
    device.fire(SCOPE_ERROR_EVENT, {"method": "x", "code": 1})
    c.disconnect()
    assert bus.log() == ()


def test_controller_attached_while_already_connected(bus, settings):
    device = FakeDevice()
    c = ConnectionController(settings, device_factory=make_factory(device))
    c.connect()
    assert c.wait(5)
    nc = NotificationController(bus, c)  # attached after the connect
    assert nc.device is device
    device.fire(STACK_FINISHED_EVENT, {"frames": 1, "lapse_ms": 10000})
    assert bus.log()[0].title == "Stack finished"


def test_bus_is_the_only_channel_no_widget_api(bus, settings):
    # no modal anything: the controller exposes no dialog/widget surface,
    # every notification goes through the bus only
    c = ConnectionController(
        settings, device_factory=make_factory(error=RuntimeError("nope"))
    )
    nc = NotificationController(bus, c)
    assert nc.bus is bus
    assert not any(
        hasattr(nc, attr) for attr in ("dialog", "toast", "overlay", "window", "present")
    )
    c.connect()
    assert c.wait(5)
    assert len(bus.log()) == 1
    assert all(isinstance(n, Notification) for n in bus.log())


# --- public API + module boundary -------------------------------------------------------


def test_public_api_is_exported_from_package():
    for name in (
        "NotificationSeverity",
        "Notification",
        "NotificationBus",
        "NotificationController",
        "NotificationListener",
        "EventSubscribe",
        "default_event_subscribe",
        "DEFAULT_LOG_CAPACITY",
        "CONNECTION_SOURCE",
        "SCOPE_ERROR_EVENT",
        "STACK_EVENT",
        "STACK_FINISHED_EVENT",
        "DEVICE_EVENTS",
    ):
        assert name in ui.__all__, name
        assert hasattr(ui, name), name
    assert ui.NotificationBus is NotificationBus
    assert ui.NotificationController is NotificationController
    assert ui.NotificationSeverity is NotificationSeverity
    assert ui.DEVICE_EVENTS == (STACK_EVENT, SCOPE_ERROR_EVENT, STACK_FINISHED_EVENT)
    assert "NotificationToasts" in ui.__all__ and "NotificationToasts" in ui._WIDGET_EXPORTS
    assert "NotificationLogDialog" in ui.__all__ and "NotificationLogDialog" in ui._WIDGET_EXPORTS


def test_pure_notifications_module_imports_no_gi_no_link_and_widget_layer_stays_lazy():
    code = (
        "import sys;"
        "import seestar_controller.ui;"
        "import seestar_controller.ui.notifications;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.ui.app' not in sys.modules, 'app imported eagerly';"
        "assert 'seestar_controller.ui.toast' not in sys.modules, 'toast imported eagerly';"
        "assert 'seestar_controller.ui.notification_log' not in sys.modules, "
        "'notification_log imported eagerly';"
        "assert 'seestar_controller.ui.connection_panel' not in sys.modules, "
        "'connection_panel imported eagerly';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


# --- integration: the REAL Device against the mock Scope on 127.0.0.1 -------------------


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


def test_real_device_against_mock_scope_drop_reconnect_and_events(
    mock_scope, keypair, tmp_path
):
    from seestar_controller.link.device import Device

    bus = NotificationBus()
    c = ConnectionController(
        _localhost_settings(tmp_path), device_factory=mock_device_factory(mock_scope, keypair)
    )
    nc = NotificationController(bus, c)
    assert nc.connection is c

    worker = c.connect()
    assert worker is not None and c.wait(10)
    assert c.status == ConnectionStatus.CONNECTED
    assert isinstance(c.device, Device)
    assert bus.log() == ()  # the initial connect is silent
    assert nc.device is c.device

    # --- the ticket demo: drop the connection, a (reconnecting) toast appears
    mock_scope.drop_connections()
    assert wait_until(lambda: c.status == ConnectionStatus.RECONNECTING, timeout=5), (
        "connection_lost did not surface as reconnecting"
    )
    lost = [n for n in bus.log() if n.title == "Link lost"]
    assert lost and lost[0].severity is NotificationSeverity.WARNING
    assert lost[0].source == CONNECTION_SOURCE

    assert wait_until(lambda: c.status == ConnectionStatus.CONNECTED, timeout=10), (
        "reconnected did not surface as connected"
    )
    reconnected = [n for n in bus.log() if n.title == "Reconnected"]
    assert reconnected and reconnected[0].severity is NotificationSeverity.INFO
    # the bus log contains the reconnecting + reconnected notifications
    titles = [n.title for n in bus.log()]
    assert "Link lost" in titles and "Reconnected" in titles

    # --- a scope error reply -> ERROR notification
    with pytest.raises(Exception):  # noqa: B017
        c.device.transport.request("mock_fail")  # non-zero reply -> RpcError
    assert wait_until(
        lambda: any(n.title == "Scope error" for n in bus.log()), timeout=5
    ), "scope_error did not surface as a notification"
    scope_error = next(n for n in bus.log() if n.title == "Scope error")
    assert scope_error.severity is NotificationSeverity.ERROR
    assert scope_error.source == SCOPE_ERROR_EVENT
    assert "mock_fail" in scope_error.message

    # --- stack events -> INFO notifications
    c.device.start_stack(10.0, 80)
    assert wait_until(
        lambda: any(n.title == "Stacking" for n in bus.log()), timeout=5
    ), "Stack progress did not surface as a notification"
    stacking = next(n for n in bus.log() if n.title == "Stacking")
    assert stacking.severity is NotificationSeverity.INFO
    assert stacking.source == STACK_EVENT
    c.device.stop_stack()
    assert wait_until(
        lambda: any(n.title == "Stack finished" for n in bus.log()), timeout=5
    ), "stack_finished did not surface as a notification"
    finished = next(n for n in bus.log() if n.title == "Stack finished")
    assert finished.severity is NotificationSeverity.INFO
    assert finished.source == STACK_FINISHED_EVENT

    c.disconnect()
    assert c.status == ConnectionStatus.DISCONNECTED
    assert nc.device is None
