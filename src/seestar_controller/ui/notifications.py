"""Pure-Python notification bus + controller for the ui shell (ticket 04).

This is the fourth PURE module of :mod:`seestar_controller.ui` (the two-layer
split required by ``.scratch/ui-shell/spec.md``): **no ``gi`` import anywhere
in this module** — stdlib only — so it is fully unit-testable headless (the
sandbox has no GTK, see docs/wiki/assumptions.md; a subprocess test enforces
the boundary). Like :mod:`.connection`, this module also does **not** import
:mod:`seestar_controller.link` at module top (the device event names are
re-declared as string constants, the same pattern :mod:`.connection` uses
for the lifecycle event names). The widget layer (:mod:`.toast`,
:mod:`.notification_log`, :mod:`.app`) imports ``gi`` at module top and is
never imported by this module or eagerly by the package ``__init__``.

What lives here — the single channel through which background events reach
the user (spec user story 8: errors shown as notifications, **no modal
dialogs** for background events):

- :class:`NotificationSeverity` — ``info`` / ``warning`` / ``error``;
- :class:`Notification` — a frozen record ``(severity, title, message,
  source, timestamp)``; ``timestamp`` defaults to now (UTC-aware);
- :class:`NotificationBus` — the publish/subscribe hub:

  - :meth:`~NotificationBus.push` appends the notification to a **bounded
    ring-buffer log** (``deque(maxlen=capacity)``, default
    :data:`DEFAULT_LOG_CAPACITY` = 200 — the oldest entries drop off) and
    notifies every registered listener **synchronously on the pushing
    thread** (bus callbacks may come from worker threads — the widget layer
    marshals them onto the main thread, see :mod:`.toast`); a listener that
    raises is logged and skipped, so one bad listener can neither block the
    remaining listeners nor make :meth:`~NotificationBus.push` raise;
  - :meth:`~NotificationBus.log` returns the log **newest-first** (what the
    notification-log dialog lists), :meth:`~NotificationBus.clear` empties
    it (silently — clearing is a user action, not a notification);
  - **dedup (documented choice):** :meth:`~NotificationBus.push` drops a
    notification that is identical to the *newest* log entry in
    ``(severity, title, message, source)`` (timestamp ignored) — repeated
    identical events (e.g. ``connection_lost`` + ``reconnecting`` both
    mapping to the same "Link lost" warning, or a flapping scope error)
    produce ONE toast/log entry instead of spamming; non-consecutive
    repeats (A, B, A) are kept. ``push`` returns whether the notification
    was stored + notified;
  - :meth:`~NotificationBus.add_listener` / :meth:`~NotificationBus.remove_listener`
    — the observer list (same idempotent, decorator-friendly pattern as
    :class:`~.connection.ConnectionController`).

- :class:`NotificationController` — translates the pure
  :class:`~.connection.ConnectionController`'s snapshots and the device's
  pushed events into bus notifications:

  - it registers itself as a listener on the connection controller, so every
    :class:`~.connection.ConnectionSnapshot` is translated:

    ============================  ==========================================
    snapshot                      notification
    ============================  ==========================================
    ``error``                     ERROR "Connection error: <message>"
    ``reconnecting``              WARNING "Link lost — reconnecting…"
    ``connected`` after a         INFO "Reconnected" (only after a
    ``reconnecting``               reconnect — the initial connect is silent,
                                  the panel already shows it)
    ``connecting`` /              (none)
    ``disconnected``
    ============================  ==========================================

  - when the connection becomes ``connected`` it subscribes to the device's
    pushed events via the **injectable** ``subscribe`` callable
    (:data:`EventSubscribe`: ``subscribe(device, event_name, handler) ->
    unsubscribe()``; the default uses the facade's
    ``add_event_handler`` / ``remove_event_handler``, tests use a fake) and
    translates them:

    ============================  ==========================================
    device event                  notification
    ============================  ==========================================
    ``Stack`` (state "working")   INFO "Stacking — N sub-frames, …"
    ``Stack`` (state "idle")      (skipped — the facade translates the final
                                  Stack event into ``stack_finished``)
    ``stack_finished``            INFO "Stack finished — N sub-frames in …"
    ``scope_error``               ERROR "Scope error — <method> failed …"
    ============================  ==========================================

  - device event handlers run on the link's reader thread, so they are
    marshalled through the controller's own ``invoke_on_main`` (default:
    direct call; the app binds it to ``GLib.idle_add`` — the same pattern as
    :class:`~.connection.ConnectionController`) before the bus is touched;
  - :meth:`~NotificationController.detach` unregisters from the connection
    controller and unsubscribes the device events (no listener leaks).

The bus is the ONLY channel to the UI: nothing in this module creates a
dialog — the widget layer shows toasts (:mod:`.toast`) and the user-invoked
log (:mod:`.notification_log`).
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping

from .connection import (
    ConnectionController,
    ConnectionSnapshot,
    ConnectionStatus,
    InvokeOnMain,
)

logger = logging.getLogger(__name__)

# -- device event names ---------------------------------------------------------
# Re-declared from :mod:`seestar_controller.link.resilience` /
# :mod:`seestar_controller.link.stacking` (this pure module must not import
# the link at module top — the same re-declaration pattern as
# :mod:`.connection` uses for the lifecycle event names).

#: Device event: the Scope replied with a non-zero code (one per error reply).
SCOPE_ERROR_EVENT = "scope_error"
#: Device event: pushed Stack progress (``state`` "working"/"idle").
STACK_EVENT = "Stack"
#: Device event: a Stack finished (the facade translates the final ``Stack``
#: event, state "idle", into this lifecycle event).
STACK_FINISHED_EVENT = "stack_finished"

#: The device events the :class:`NotificationController` subscribes to, in order.
DEVICE_EVENTS: tuple[str, ...] = (STACK_EVENT, SCOPE_ERROR_EVENT, STACK_FINISHED_EVENT)

#: The ``source`` of connection-derived notifications.
CONNECTION_SOURCE = "connection"

#: Default capacity of the :class:`NotificationBus` ring-buffer log.
DEFAULT_LOG_CAPACITY = 200


class NotificationSeverity(str, Enum):
    """How severe a :class:`Notification` is (drives the toast timeout)."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.value


@dataclass(frozen=True)
class Notification:
    """One immutable notification record.

    - ``severity`` — a :class:`NotificationSeverity` (a plain string
      ``"info"``/``"warning"``/``"error"`` is coerced);
    - ``title`` — the short headline ("Connection error", "Link lost", …);
    - ``message`` — the detail text (may be empty);
    - ``source`` — where it came from: :data:`CONNECTION_SOURCE` for
      connection-derived notifications, the device event name
      (``"Stack"`` / ``"scope_error"`` / ``"stack_finished"``) otherwise;
    - ``timestamp`` — when it was created; defaults to now (UTC-aware); a
      naive datetime is assumed to be UTC.
    """

    severity: NotificationSeverity
    title: str
    message: str
    source: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if isinstance(self.severity, str):
            object.__setattr__(self, "severity", NotificationSeverity(self.severity))
        if not isinstance(self.severity, NotificationSeverity):
            raise ValueError(f"severity must be a NotificationSeverity, got {self.severity!r}")
        for name in ("title", "message", "source"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be a string, got {getattr(self, name)!r}")
        if not isinstance(self.timestamp, datetime):
            raise ValueError(f"timestamp must be a datetime, got {self.timestamp!r}")
        if self.timestamp.tzinfo is None:
            object.__setattr__(
                self, "timestamp", self.timestamp.replace(tzinfo=timezone.utc)
            )


def _dedup_key(notification: Notification) -> tuple:
    """The identity used for consecutive-duplicate dedup (timestamp ignored)."""
    return (notification.severity, notification.title, notification.message, notification.source)


#: A notification listener: called with the pushed :class:`Notification`
#: (synchronously, on the pushing thread — marshal in the listener if needed).
NotificationListener = Callable[[Notification], None]


class NotificationBus:
    """The publish/subscribe hub + bounded notification log.

    ``capacity`` bounds the ring-buffer log (:meth:`log`); the oldest
    entries drop off when it is full. ``capacity`` must be >= 1.

    Thread-safety: the log and the listener list are lock-protected;
    listeners are called synchronously on the pushing thread, OUTSIDE the
    lock (a listener must not call back into the bus re-entrantly from the
    same thread while holding it — the lock is only held for the append).
    A listener that raises is logged (module logger) and skipped: the
    remaining listeners still run and :meth:`push` still returns ``True``
    (the same isolation pattern as the link transport's event dispatch,
    :mod:`seestar_controller.link.transport`).
    """

    def __init__(self, capacity: int = DEFAULT_LOG_CAPACITY) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError(f"capacity must be an int >= 1, got {capacity!r}")
        self._capacity = capacity
        self._log: deque[Notification] = deque(maxlen=capacity)
        self._listeners: list[NotificationListener] = []
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        """The ring-buffer capacity of the log."""
        return self._capacity

    @property
    def listeners(self) -> tuple[NotificationListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    def add_listener(self, listener: NotificationListener) -> NotificationListener:
        """Register an observer callback (idempotent); returns it, so it can
        be used as a decorator."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: NotificationListener) -> None:
        """Unregister an observer callback (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def push(self, notification: Notification) -> bool:
        """Store ``notification`` in the log and notify the listeners.

        Returns ``True`` when the notification was stored + notified.
        Returns ``False`` (nothing stored, nobody notified) when it is a
        consecutive duplicate of the newest log entry — same
        ``(severity, title, message, source)``, timestamp ignored
        (documented dedup choice, see module docstring).

        A listener that raises is logged (module logger) and skipped: it
        neither prevents the remaining listeners from being notified nor
        changes the return value — the notification stays stored, so it is
        in :meth:`log` and can never be re-delivered by dedup.
        """
        if not isinstance(notification, Notification):
            raise ValueError(f"push expects a Notification, got {notification!r}")
        with self._lock:
            if self._log and _dedup_key(self._log[-1]) == _dedup_key(notification):
                return False
            self._log.append(notification)
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(notification)
            except Exception:
                logger.exception("notification listener %r failed", listener)
        return True

    def log(self) -> tuple[Notification, ...]:
        """The stored notifications, **newest-first**."""
        with self._lock:
            return tuple(reversed(self._log))

    def clear(self) -> None:
        """Empty the log (silently — no listener notification; clearing is a
        user action on the log dialog, not a notification)."""
        with self._lock:
            self._log.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._log)


def _direct_invoke(callback: Callable[[], None]) -> None:
    """The default ``invoke_on_main``: run the callback directly (tests)."""
    callback()
    return None


#: An event subscription: ``subscribe(device, event_name, handler)`` registers
#: ``handler`` for ``event_name`` on ``device`` and returns an **unsubscribe**
#: zero-argument callable. The default uses the device facade's
#: ``add_event_handler`` / ``remove_event_handler``; tests inject a fake.
EventSubscribe = Callable[[Any, str, Callable[[Any], None]], Callable[[], None]]


def default_event_subscribe(
    device: Any, name: str, handler: Callable[[Any], None]
) -> Callable[[], None]:
    """The default :data:`EventSubscribe`: the device facade's event registry."""
    device.add_event_handler(name, handler)

    def unsubscribe() -> None:
        device.remove_event_handler(name, handler)

    return unsubscribe


def _event_payload(event: Any) -> Mapping[str, Any]:
    """The payload of a device event (duck-typed: ``event.payload``)."""
    payload = getattr(event, "payload", None)
    return payload if isinstance(payload, Mapping) else {}


def _format_result(result: Any) -> str:
    """Format a scope_error ``result`` for a notification message."""
    if result is None:
        return ""
    if isinstance(result, Mapping):
        return json.dumps(result, sort_keys=True, default=str)
    return str(result)


class NotificationController:
    """Translates connection snapshots + device events into bus notifications.

    - ``bus`` — the :class:`NotificationBus` to push to (the ONLY channel to
      the UI — this controller never creates widgets or dialogs);
    - ``connection`` — the :class:`~.connection.ConnectionController` to
      observe (its listener callbacks already run via its own
      ``invoke_on_main``);
    - ``invoke_on_main`` — marshals the device event handlers (which run on
      the link's reader thread) before the bus is touched; the app binds it
      to ``GLib.idle_add``;
    - ``subscribe`` — the injectable :data:`EventSubscribe` used to arm the
      device event handlers (default: the facade's ``add_event_handler``).

    See the module docstring for the translation tables. Use
    :meth:`detach` to unregister everything.
    """

    def __init__(
        self,
        bus: NotificationBus,
        connection: ConnectionController,
        *,
        invoke_on_main: InvokeOnMain | None = None,
        subscribe: EventSubscribe | None = None,
    ) -> None:
        self._bus = bus
        self._connection = connection
        self._invoke_on_main: InvokeOnMain = invoke_on_main or _direct_invoke
        self._subscribe: EventSubscribe = subscribe or default_event_subscribe
        self._device: Any | None = None
        self._unsubscribers: list[Callable[[], None]] = []
        self._last_status: ConnectionStatus = connection.status
        connection.add_listener(self._on_snapshot)
        # already connected when attached? arm the device events right away
        if connection.status == ConnectionStatus.CONNECTED:
            self._sync_device(connection.device)

    @property
    def bus(self) -> NotificationBus:
        """The bus this controller pushes to."""
        return self._bus

    @property
    def connection(self) -> ConnectionController:
        """The observed connection controller."""
        return self._connection

    @property
    def device(self) -> Any | None:
        """The device whose events are currently subscribed (None when not
        connected)."""
        return self._device

    def detach(self) -> None:
        """Unregister from the connection controller and unsubscribe the
        device events (idempotent)."""
        self._connection.remove_listener(self._on_snapshot)
        self._unsubscribe_device()

    # -- connection snapshots ---------------------------------------------------

    def _on_snapshot(self, snapshot: ConnectionSnapshot) -> None:
        """Connection-controller listener: translate the snapshot into a
        notification and keep the device event subscription in sync."""
        status = snapshot.status
        if status == ConnectionStatus.CONNECTED:
            self._sync_device(self._connection.device)
            if self._last_status == ConnectionStatus.RECONNECTING:
                self._bus.push(
                    Notification(
                        NotificationSeverity.INFO,
                        "Reconnected",
                        f"Link to {snapshot.scope_ip} restored",
                        CONNECTION_SOURCE,
                    )
                )
        elif status == ConnectionStatus.RECONNECTING:
            self._bus.push(
                Notification(
                    NotificationSeverity.WARNING,
                    "Link lost",
                    f"Reconnecting to {snapshot.scope_ip}…",
                    CONNECTION_SOURCE,
                )
            )
        elif status == ConnectionStatus.ERROR:
            self._unsubscribe_device()
            self._bus.push(
                Notification(
                    NotificationSeverity.ERROR,
                    "Connection error",
                    snapshot.error or "unknown error",
                    CONNECTION_SOURCE,
                )
            )
        elif status == ConnectionStatus.DISCONNECTED:
            self._unsubscribe_device()
        self._last_status = status

    # -- device events ------------------------------------------------------------

    def _sync_device(self, device: Any | None) -> None:
        """Subscribe to ``device``'s events when it is not the one already
        subscribed; unsubscribe the previous device first."""
        if device is self._device:
            return
        self._unsubscribe_device()
        if device is None:
            return
        self._device = device
        for name in DEVICE_EVENTS:
            handler = self._make_device_handler(name)
            try:
                unsubscribe = self._subscribe(device, name, handler)
            except Exception:
                continue  # a device without this event must not break the rest
            self._unsubscribers.append(unsubscribe)

    def _unsubscribe_device(self) -> None:
        """Call every device-event unsubscribe callable (best effort)."""
        unsubscribers, self._unsubscribers = self._unsubscribers, []
        self._device = None
        for unsubscribe in unsubscribers:
            try:
                unsubscribe()
            except Exception:
                pass

    def _make_device_handler(self, name: str) -> Callable[[Any], None]:
        """Build the handler for device event ``name``; the event is
        marshalled via ``invoke_on_main`` before the bus is touched."""

        def on_event(event: Any) -> None:
            self._invoke_on_main(lambda: self._on_device_event(name, event))

        return on_event

    def _on_device_event(self, name: str, event: Any) -> None:
        """Translate a device event (runs via ``invoke_on_main``) into a bus
        notification; see the module docstring for the table."""
        payload = _event_payload(event)
        if name == SCOPE_ERROR_EVENT:
            method = str(payload.get("method", "request"))
            code = payload.get("code", "?")
            detail = _format_result(payload.get("result"))
            message = f"{method} failed (code {code})"
            if detail:
                message = f"{message}: {detail}"
            self._bus.push(
                Notification(NotificationSeverity.ERROR, "Scope error", message, name)
            )
        elif name == STACK_EVENT:
            if str(payload.get("state", "")) == "idle":
                return  # the final Stack event — surfaced as stack_finished
            frames = payload.get("frames", 0)
            lapse_ms = payload.get("lapse_ms", 0)
            self._bus.push(
                Notification(
                    NotificationSeverity.INFO,
                    "Stacking",
                    f"{frames} sub-frames stacked, {lapse_ms} ms elapsed",
                    name,
                )
            )
        elif name == STACK_FINISHED_EVENT:
            frames = payload.get("frames", 0)
            lapse_ms = payload.get("lapse_ms", 0)
            self._bus.push(
                Notification(
                    NotificationSeverity.INFO,
                    "Stack finished",
                    f"{frames} sub-frames in {lapse_ms} ms",
                    name,
                )
            )
