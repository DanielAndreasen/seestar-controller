"""Resilience vocabulary for the Link: connection states, reconnect policy,
and the unified lifecycle event names.

The Link (GLOSSARY.md) can drop at any moment — WiFi blip, scope reboot,
cable — detected via socket EOF or heartbeat silence (see
:mod:`seestar_controller.link.transport`). This module defines:

- :class:`ConnectionState` — the caller-visible state of the command
  channel: ``disconnected`` / ``connecting`` / ``connected`` /
  ``reconnecting``;
- :class:`ReconnectPolicy` — the knobs for automatic reconnect with
  exponential backoff (``max_attempts``, ``base_delay``, ``cap``);
- the lifecycle event names dispatched through the transport's unified
  event dispatch (:meth:`LinkTransport.add_event_handler`), so callers
  register one handler registry for wire events *and* link lifecycle:

  ====================  ==================================================
  event name            occurrence
  ====================  ==================================================
  ``connection_lost``   the Link dropped (EOF, heartbeat silence, ...)
  ``reconnecting``      a reconnect cycle started (once per loss)
  ``reconnected``       the Link came back (handshake re-run), payload
                        carries ``attempts``
  ``scope_error``       the Scope replied with a non-zero code (one per
                        error reply), payload carries id/method/code/result
  ``stack_finished``    a final ``Stack`` event arrived (state "idle"),
                        payload carries frames/lapse_ms
  ====================  ==================================================

  Each event is dispatched to registered handlers **exactly once per
  occurrence** — e.g. a loss detected simultaneously by the reader (EOF)
  and the heartbeat thread produces a single ``connection_lost``.

NOTE (user AFK, no real scope — docs/wiki/assumptions.md): reconnect is
exercised against the mock Scope only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ConnectionState(str, Enum):
    """Caller-visible state of the Link's command channel."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.value


#: Lifecycle event: the Link dropped (socket EOF, heartbeat silence, send
#: failure). Payload: ``{"reason": str}``.
CONNECTION_LOST_EVENT = "connection_lost"
#: Lifecycle event: a reconnect cycle started. Payload: ``{"reason": str,
#: "max_attempts": int | None, "base_delay": float, "cap": float}``.
RECONNECTING_EVENT = "reconnecting"
#: Lifecycle event: the Link reconnected and the signed handshake re-ran.
#: Payload: ``{"attempts": int, "reason": str}``.
RECONNECTED_EVENT = "reconnected"
#: Error event: the Scope replied with a non-zero ``code``. Payload:
#: ``{"id": int, "method": str, "code": int, "result": Any}``.
SCOPE_ERROR_EVENT = "scope_error"
#: Event: a Stack finished (final ``Stack`` event, state "idle"). Payload:
#: ``{"state": str, "lapse_ms": int, "frames": int}``.
STACK_FINISHED_EVENT = "stack_finished"

#: All lifecycle/error event names, in lifecycle order.
LIFECYCLE_EVENTS = (
    CONNECTION_LOST_EVENT,
    RECONNECTING_EVENT,
    RECONNECTED_EVENT,
    SCOPE_ERROR_EVENT,
    STACK_FINISHED_EVENT,
)


@dataclass(frozen=True)
class ReconnectPolicy:
    """Knobs for automatic reconnect with exponential backoff.

    After a connection loss the transport waits ``base_delay`` seconds,
    tries to reconnect (re-running the signed handshake), and on failure
    waits ``base_delay * factor ** (attempt - 1)`` seconds before the next
    attempt, capped at ``cap``. ``max_attempts`` bounds the attempts per
    reconnect cycle (``None`` = retry forever, the default for an
    observing session); when the bound is hit the transport gives up and
    goes to ``disconnected``.
    """

    #: Maximum reconnect attempts per cycle; ``None`` = unlimited.
    max_attempts: int | None = None
    #: Delay before the first reconnect attempt, in seconds.
    base_delay: float = 0.5
    #: Upper bound for the backoff delay, in seconds.
    cap: float = 30.0
    #: Backoff multiplier applied per failed attempt.
    factor: float = 2.0

    def __post_init__(self) -> None:
        if self.max_attempts is not None and self.max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1 or None, got {self.max_attempts}")
        if self.base_delay <= 0:
            raise ValueError(f"base_delay must be > 0, got {self.base_delay}")
        if self.cap <= 0:
            raise ValueError(f"cap must be > 0, got {self.cap}")
        if self.factor < 1.0:
            raise ValueError(f"factor must be >= 1.0, got {self.factor}")

    def delay_for(self, attempt: int) -> float:
        """The backoff delay before ``attempt`` (1-based), capped at ``cap``."""
        if attempt < 1:
            raise ValueError(f"attempt must be >= 1, got {attempt}")
        return min(self.cap, self.base_delay * (self.factor ** (attempt - 1)))
