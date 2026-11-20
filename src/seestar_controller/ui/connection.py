"""Pure-Python connection controller for the ui shell (ticket 03).

This is the third PURE module of :mod:`seestar_controller.ui` (the two-layer
split required by ``.scratch/ui-shell/spec.md``): **no ``gi`` import anywhere
in this module** — stdlib only — so it is fully unit-testable headless (the
sandbox has no GTK, see docs/wiki/assumptions.md; a subprocess test enforces
the boundary). Like :mod:`.state`, this module also does **not** import
:mod:`seestar_controller.link` at module top (the link is imported lazily
inside the default device factory only), so importing the pure ui layer
stays light. The widget layer (:mod:`.connection_panel`, :mod:`.app`)
imports ``gi`` at module top and is never imported by this module or
eagerly by the package ``__init__``.

What lives here — the connection state machine wired to the device facade:

- :class:`ConnectionStatus` — the caller-visible state:
  ``disconnected`` / ``connecting`` / ``connected`` / ``error`` (the four
  ticket states) plus ``reconnecting``, surfaced from the Link's resilience
  lifecycle (ticket 07 of seestar-link): when the Link drops while connected,
  the facade reconnects by default, so the panel shows ``reconnecting``
  until the ``reconnected`` event arrives;
- :class:`ConnectionSnapshot` — an immutable view of the controller
  (status, scope IP, scope state, error) handed to listeners;
- :class:`ConnectionController` — the state machine:

  - :meth:`~ConnectionController.connect` reads the scope IP (and PEM key
    path) from the settings (:class:`~.settings.SettingsController` or
    :class:`~.state.AppState`), validates it (empty/invalid IP -> ``error``
    state WITHOUT calling the factory), then runs the blocking
    ``Device.connect`` on a **worker thread** (:class:`threading.Thread`)
    so the UI never freezes (spec user story 7). The worker's result is
    marshalled back through the ``invoke_on_main`` callable — the widget
    layer binds it to ``GLib.idle_add``; the default is a direct call, so
    the pure layer itself stays GLib-free. Success -> ``connected`` (the
    device and its :class:`~seestar_controller.link.device.DeviceState`
    snapshot are stored); failure -> ``error`` with the exception message;
  - :meth:`~ConnectionController.disconnect` -> ``disconnected`` (closes
    the device, disarms the lifecycle handlers; idempotent);
  - device lifecycle events (``connection_lost`` / ``reconnecting`` /
    ``reconnected``, registered via the device's ``add_event_handler``) are
    marshalled the same way and update the state: loss/reconnecting ->
    ``reconnecting``, reconnected -> ``connected`` (with a refreshed scope
    state);
  - ``add_listener`` / ``remove_listener`` — the observer list (same pattern
    as :class:`~.settings.SettingsController`); every state change notifies
    each listener with a :class:`ConnectionSnapshot`. Errors surface through
    these notifications for now — ticket 04 builds the notification bus
    (banners/toasts) on top of them;
  - ``device_factory`` is injectable (constructor or per-:meth:`connect`
    call): ``factory(scope_ip, key_path) -> device`` where ``device`` is
    anything with the facade shape (``state()``, ``disconnect()``,
    ``add_event_handler`` / ``remove_event_handler``) — tests drive it with
    a fake, or with the real :class:`~seestar_controller.link.device.Device`
    against :mod:`~seestar_controller.link.mock_scope`. The default factory
    lazily imports the link and calls ``Device.connect(ip, pem_path=...)``.

Documented choices:

- **Double connect is rejected**: :meth:`connect` raises
  :class:`ConnectionStateError` while ``connecting`` / ``connected`` /
  ``reconnecting`` (disconnect first); from ``disconnected`` or ``error``
  it (re)starts a connect. A generation counter invalidates in-flight
  results: ``disconnect()`` during a pending connect discards the late
  result (and disconnects the stale device) instead of flipping the state
  back to ``connected``.
- **All worker/lifecycle results are applied inside the marshalled
  callback**, so with ``GLib.idle_add`` the state mutation + listener
  notification happen on the main thread; with the default direct
  ``invoke_on_main`` everything is synchronous once the worker finishes
  (tests use :meth:`~ConnectionController.wait` to join the worker).
- The scope state is treated **opaquely** (duck-typed) — the pure layer
  never imports the link's :class:`DeviceState`; :func:`describe_scope_state`
  formats it for display.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .state import SettingsValidationError, UIStateError, validate_scope_ip

#: Lifecycle event names, re-declared from
#: :mod:`seestar_controller.link.resilience` (this pure module must not
#: import the link at module top — the same re-declaration pattern as
#: :mod:`.state` uses for the config-dir constants).
CONNECTION_LOST_EVENT = "connection_lost"
RECONNECTING_EVENT = "reconnecting"
RECONNECTED_EVENT = "reconnected"

#: The device lifecycle events the controller subscribes to, in order.
LIFECYCLE_EVENTS: tuple[str, ...] = (
    CONNECTION_LOST_EVENT,
    RECONNECTING_EVENT,
    RECONNECTED_EVENT,
)


class ConnectionStatus(str, Enum):
    """Caller-visible state of the connection to the Scope."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.value


class ConnectionStateError(UIStateError, RuntimeError):
    """An invalid connection-controller transition (e.g. a double connect).

    Subclasses both :class:`~.state.UIStateError` and :class:`RuntimeError`.
    """


@dataclass(frozen=True)
class ConnectionSnapshot:
    """An immutable view of the connection controller, handed to listeners.

    - ``status`` — the :class:`ConnectionStatus`;
    - ``scope_ip`` — the scope address of the current/last connect attempt;
    - ``scope_state`` — the Scope's device state snapshot on a successful
      connect (opaque to this module — a
      :class:`~seestar_controller.link.device.DeviceState` in the app,
      anything duck-typed in tests), ``None`` otherwise;
    - ``error`` — the failure message in the ``error`` state, else ``None``.
    """

    status: ConnectionStatus
    scope_ip: str
    scope_state: Any | None
    error: str | None


#: A connection-change listener: called with a :class:`ConnectionSnapshot`
#: after every state change (always via ``invoke_on_main``).
ConnectionListener = Callable[[ConnectionSnapshot], None]

#: A device factory: ``factory(scope_ip, key_path) -> device``. The returned
#: device must provide ``state()``, ``disconnect()``,
#: ``add_event_handler(name, handler)`` and ``remove_event_handler(name,
#: handler)`` — the :class:`~seestar_controller.link.device.Device` facade
#: shape.
DeviceFactory = Callable[[str, str], Any]

#: Marshals a zero-argument callback onto the "main" thread. The widget
#: layer binds this to ``GLib.idle_add``; the default calls it directly.
InvokeOnMain = Callable[[Callable[[], None]], Any]


def _direct_invoke(callback: Callable[[], None]) -> None:
    """The default ``invoke_on_main``: run the callback directly (tests)."""
    callback()


def default_device_factory(scope_ip: str, key_path: str) -> Any:
    """The default device factory: the real Device facade.

    Imports the link lazily (this pure module must not import
    :mod:`seestar_controller.link` at module top). ``key_path`` empty
    (unset) -> the transport's own key-path resolution
    (``SEESTAR_KEY_PATH`` > ``~/.config/seestar-controller/key.pem``); a
    missing/invalid key raises ``MissingKeyError``/``InvalidKeyError``,
    which the controller surfaces as the ``error`` state.
    """
    from seestar_controller.link.device import Device

    kwargs: dict[str, Any] = {}
    if key_path:
        kwargs["pem_path"] = key_path
    return Device.connect(scope_ip, **kwargs)


def describe_scope_state(scope_state: Any) -> str:
    """Format a scope-state snapshot for display (duck-typed, pure).

    ``"mode idle, target ra 5.50 h, dec +12.25°"`` when a target is set,
    ``"mode idle, no target"`` otherwise, ``"no scope state"`` for None.
    """
    if scope_state is None:
        return "no scope state"
    mode = getattr(scope_state, "mode", "unknown")
    target = getattr(scope_state, "target", None)
    if target:
        try:
            ra, dec = float(target[0]), float(target[1])
        except (TypeError, ValueError, IndexError):
            return f"mode {mode}"
        return f"mode {mode}, target ra {ra:.2f} h, dec {dec:+.2f}°"
    return f"mode {mode}, no target"


#: Sentinel for "leave the field unchanged" in :meth:`ConnectionController._transition`.
_UNSET: Any = object()


class ConnectionController:
    """The connection state machine: settings -> worker thread -> facade.

    - ``settings`` — a :class:`~.settings.SettingsController` (or a plain
      :class:`~.state.AppState`); the scope IP and PEM key path are read
      from it at every :meth:`connect` call, so settings changes apply to
      the next connect;
    - ``device_factory`` — the default factory (per-call override via
      :meth:`connect`); see :data:`DeviceFactory`;
    - ``invoke_on_main`` — the marshalling callable (default: direct call);
      the widget layer binds it to ``GLib.idle_add``.

    State transitions (all notified to the listeners via ``invoke_on_main``):

    ============================  =========================================
    transition                    result
    ============================  =========================================
    :meth:`connect`, valid IP     ``connecting`` -> worker -> ``connected``
                                  (device + scope state stored) or ``error``
    :meth:`connect`, empty/bad IP ``error`` immediately, factory NOT called
    :meth:`disconnect`            ``disconnected`` (idempotent)
    ``connection_lost`` event     ``reconnecting`` (while connected)
    ``reconnecting`` event        ``reconnecting``
    ``reconnected`` event         ``connected`` + refreshed scope state
    ============================  =========================================
    """

    def __init__(
        self,
        settings: Any,
        *,
        device_factory: DeviceFactory | None = None,
        invoke_on_main: InvokeOnMain | None = None,
    ) -> None:
        self._settings = settings
        self._device_factory: DeviceFactory = device_factory or default_device_factory
        self._invoke_on_main: InvokeOnMain = invoke_on_main or _direct_invoke
        self._lock = threading.Lock()
        self._status = ConnectionStatus.DISCONNECTED
        self._device: Any | None = None
        self._scope_state: Any | None = None
        self._error: str | None = None
        self._scope_ip = ""
        self._generation = 0
        self._worker: threading.Thread | None = None
        self._listeners: list[ConnectionListener] = []
        #: device -> [(event name, handler)] currently armed on that device
        self._lifecycle_handlers: dict[Any, list[tuple[str, Callable]]] = {}

    # -- settings -------------------------------------------------------------

    @property
    def settings(self) -> Any:
        """The settings source (a SettingsController or an AppState)."""
        return self._settings

    def _settings_values(self) -> dict[str, str]:
        """The current settings dict (SettingsController.values / AppState.settings)."""
        values = getattr(self._settings, "values", None)
        if values is not None:
            return dict(values)  # SettingsController
        return dict(self._settings.settings)  # AppState

    # -- state ----------------------------------------------------------------

    @property
    def status(self) -> ConnectionStatus:
        """The current :class:`ConnectionStatus`."""
        with self._lock:
            return self._status

    @property
    def device(self) -> Any | None:
        """The connected device facade (None unless ``connected``)."""
        with self._lock:
            return self._device

    @property
    def scope_state(self) -> Any | None:
        """The Scope's state snapshot from the successful connect (or the
        refresh after a ``reconnected`` event); None otherwise."""
        with self._lock:
            return self._scope_state

    @property
    def error(self) -> str | None:
        """The failure message in the ``error`` state, else None."""
        with self._lock:
            return self._error

    @property
    def scope_ip(self) -> str:
        """The scope address of the current/last connect attempt."""
        with self._lock:
            return self._scope_ip

    @property
    def snapshot(self) -> ConnectionSnapshot:
        """An immutable :class:`ConnectionSnapshot` of the current state."""
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> ConnectionSnapshot:
        return ConnectionSnapshot(
            status=self._status,
            scope_ip=self._scope_ip,
            scope_state=self._scope_state,
            error=self._error,
        )

    # -- listeners --------------------------------------------------------------

    @property
    def listeners(self) -> tuple[ConnectionListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    def add_listener(self, listener: ConnectionListener) -> ConnectionListener:
        """Register an observer callback (idempotent); returns it, so it can
        be used as a decorator."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: ConnectionListener) -> None:
        """Unregister an observer callback (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self, snapshot: ConnectionSnapshot) -> None:
        """Call every listener with ``snapshot`` (runs via ``invoke_on_main``)."""
        for listener in list(self._listeners):
            listener(snapshot)

    # -- connect / disconnect ---------------------------------------------------

    def connect(
        self,
        scope_ip: str | None = None,
        *,
        device_factory: DeviceFactory | None = None,
    ) -> threading.Thread | None:
        """Start connecting to the Scope; returns the worker thread.

        ``scope_ip`` defaults to the ``scope_ip`` setting. The blocking
        factory call runs on a worker thread; its result is marshalled back
        via ``invoke_on_main`` and applied there (``connected`` with the
        device + scope state, or ``error`` with the message).

        Returns the worker :class:`threading.Thread`, or ``None`` when the
        IP was empty/invalid — in that case the ``error`` state is set
        synchronously and the factory is NOT called.

        Raises :class:`ConnectionStateError` when called while
        ``connecting`` / ``connected`` / ``reconnecting`` (documented
        choice: a double connect is rejected — disconnect first).
        """
        factory = device_factory if device_factory is not None else self._device_factory
        with self._lock:
            if self._status in (
                ConnectionStatus.CONNECTING,
                ConnectionStatus.CONNECTED,
                ConnectionStatus.RECONNECTING,
            ):
                raise ConnectionStateError(
                    f"cannot connect while {self._status.value} — disconnect first"
                )
            self._generation += 1
            generation = self._generation
        values = self._settings_values()
        ip = scope_ip if scope_ip is not None else values.get("scope_ip", "")
        key_path = values.get("key_path", "")
        try:
            ip = validate_scope_ip(ip)
        except SettingsValidationError as exc:
            self._transition(generation, ConnectionStatus.ERROR, error=str(exc), scope_ip=str(ip))
            return None
        if not ip:
            self._transition(
                generation,
                ConnectionStatus.ERROR,
                error="scope IP is not set — open Settings and set the scope address",
                scope_ip="",
            )
            return None
        self._transition(generation, ConnectionStatus.CONNECTING, error=None, scope_ip=ip)
        worker = threading.Thread(
            target=self._connect_worker,
            args=(generation, ip, key_path, factory),
            name="seestar-ui-connect",
            daemon=True,
        )
        with self._lock:
            self._worker = worker
        worker.start()
        return worker

    def disconnect(self) -> None:
        """Disconnect: -> ``disconnected`` (idempotent).

        Closes the device, disarms the lifecycle handlers, and invalidates
        any in-flight connect (a late worker result is discarded and its
        stale device disconnected). A no-op (no notification) when already
        fully disconnected.
        """
        with self._lock:
            self._generation += 1
            device = self._device
            changed = (
                self._status != ConnectionStatus.DISCONNECTED
                or device is not None
                or self._error is not None
            )
            self._status = ConnectionStatus.DISCONNECTED
            self._device = None
            self._scope_state = None
            self._error = None
            snapshot = self._snapshot_locked() if changed else None
        if device is not None:
            self._disarm(device)
            try:
                device.disconnect()
            except Exception:  # noqa: BLE001, S110
                pass  # a failing disconnect must not break the state machine
        if snapshot is not None:
            self._invoke_on_main(lambda: self._notify(snapshot))

    def wait(self, timeout: float | None = None) -> bool:
        """Join the in-flight connect worker (if any); True when no worker
        is running (or it finished within ``timeout``).

        With the default direct ``invoke_on_main`` the result is already
        applied when :meth:`wait` returns True; with an async marshalling
        (``GLib.idle_add``) the transition is applied when the main loop
        runs the queued callback.
        """
        with self._lock:
            worker = self._worker
        if worker is None:
            return True
        worker.join(timeout)
        return not worker.is_alive()

    # -- internals ----------------------------------------------------------------

    def _transition(
        self,
        generation: int,
        status: ConnectionStatus,
        *,
        error: Any = _UNSET,
        scope_state: Any = _UNSET,
        scope_ip: Any = _UNSET,
    ) -> ConnectionSnapshot | None:
        """Apply a synchronous transition (called on the caller's thread) and
        marshal the listener notification via ``invoke_on_main``."""
        with self._lock:
            if generation != self._generation:
                return None  # superseded
            self._status = status
            if error is not _UNSET:
                self._error = error
            if scope_state is not _UNSET:
                self._scope_state = scope_state
            if scope_ip is not _UNSET:
                self._scope_ip = scope_ip
            snapshot = self._snapshot_locked()
        self._invoke_on_main(lambda: self._notify(snapshot))
        return snapshot

    def _connect_worker(
        self, generation: int, ip: str, key_path: str, factory: DeviceFactory
    ) -> None:
        """Worker thread body: run the blocking factory + read the scope
        state, then marshal the result back via ``invoke_on_main``."""
        device = None
        scope_state = None
        error: str | None = None
        try:
            device = factory(ip, key_path)
            scope_state = device.state()
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            if device is not None:
                # the factory succeeded but the state read failed: close the
                # half-open device instead of leaking it
                try:
                    device.disconnect()
                except Exception:  # noqa: BLE001, S110
                    pass
                device = None
                scope_state = None
        if device is not None:
            self._arm(device)  # outside self._lock (device calls must not run under it)
        self._invoke_on_main(
            lambda: self._apply_connect_result(generation, device, scope_state, error)
        )

    def _apply_connect_result(
        self,
        generation: int,
        device: Any | None,
        scope_state: Any | None,
        error: str | None,
    ) -> None:
        """Apply a worker result (runs via ``invoke_on_main``): ``connected``
        with device + scope state, or ``error`` with the message. A stale
        generation (disconnect() happened meanwhile) discards the result and
        disconnects the stale device."""
        with self._lock:
            if generation != self._generation:
                stale_device = device
                snapshot = None
            else:
                stale_device = None
                if error is not None:
                    self._status = ConnectionStatus.ERROR
                    self._error = error
                    self._device = None
                    self._scope_state = None
                else:
                    self._status = ConnectionStatus.CONNECTED
                    self._error = None
                    self._device = device
                    self._scope_state = scope_state
                snapshot = self._snapshot_locked()
        if stale_device is not None:
            self._disarm(stale_device)
            try:
                stale_device.disconnect()
            except Exception:  # noqa: BLE001, S110
                pass
            return
        self._notify(snapshot)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    # -- device lifecycle events ----------------------------------------------------

    def _arm(self, device: Any) -> None:
        """Subscribe to the device's lifecycle events (outside ``self._lock``)."""
        handlers: list[tuple[str, Callable]] = []
        for name in LIFECYCLE_EVENTS:
            handler = self._make_lifecycle_handler(device, name)
            device.add_event_handler(name, handler)
            handlers.append((name, handler))
        with self._lock:
            self._lifecycle_handlers[device] = handlers

    def _disarm(self, device: Any) -> None:
        """Remove the lifecycle handlers registered on ``device``."""
        with self._lock:
            handlers = self._lifecycle_handlers.pop(device, [])
        for name, handler in handlers:
            try:
                device.remove_event_handler(name, handler)
            except Exception:  # noqa: BLE001, S110
                pass

    def _make_lifecycle_handler(self, device: Any, name: str) -> Callable:
        """Build the event handler for ``name`` on ``device``; the event is
        marshalled via ``invoke_on_main`` before the state is touched."""

        def on_event(event: Any) -> None:
            self._invoke_on_main(lambda: self._apply_lifecycle(device, name, event))

        return on_event

    def _apply_lifecycle(self, device: Any, name: str, event: Any) -> None:
        """Apply a device lifecycle event (runs via ``invoke_on_main``):
        ``connection_lost`` / ``reconnecting`` -> ``reconnecting``,
        ``reconnected`` -> ``connected`` with a refreshed scope state.
        Events from a stale device (after disconnect) are ignored."""
        with self._lock:
            if self._device is not device:
                return  # stale device
            if name in (CONNECTION_LOST_EVENT, RECONNECTING_EVENT):
                self._status = ConnectionStatus.RECONNECTING
                self._error = None
            elif name == RECONNECTED_EVENT:
                self._status = ConnectionStatus.CONNECTED
                self._error = None
            else:
                return
            snapshot = self._snapshot_locked()
        if name == RECONNECTED_EVENT:
            # Refresh the scope state on the fresh link; keep the last known
            # one when the read fails (documented choice).
            try:
                refreshed = device.state()
            except Exception:  # noqa: BLE001
                refreshed = None
            if refreshed is not None:
                with self._lock:
                    if self._device is device:
                        self._scope_state = refreshed
                        snapshot = self._snapshot_locked()
        self._notify(snapshot)
