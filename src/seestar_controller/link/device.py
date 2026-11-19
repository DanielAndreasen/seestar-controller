"""Device facade: high-level Scope operations on top of the Link transport.

:class:`Device` is the object the rest of the app talks to. It wraps a
connected :class:`~seestar_controller.link.transport.LinkTransport` (the
Link's command channel, TCP :4700) and exposes the Scope's device-level
operations in GLOSSARY vocabulary:

- :meth:`Device.connect` — ``connect(ip)`` opens the Link (PEM-signed
  handshake, firmware >= 7.18 incl. 9.75), reads the Scope's live state,
  and returns the facade;
- :meth:`Device.state` — the Scope's current **mode**, current **target**
  (ra, dec) and **stacking** progress, via the ``get_scope_state`` RPC;
- :meth:`Device.goto` — slew to a target's RA/Dec (``scope_goto_target``);
  arrival is reported by the pushed ``ScopeGoto`` "complete" event, with a
  state-polling fallback if the event is missed;
- :meth:`Device.park` — park the Scope (``scope_park``, mode -> parked).

Pushed events from the Scope are surfaced through registered callbacks
(:meth:`Device.add_event_handler` / :meth:`Device.on_event`), reusing the
transport's event dispatch.

NOTE (user AFK, no real scope / no PEM key — docs/wiki/assumptions.md):
the facade is exercised against the mock Scope
(:mod:`seestar_controller.link.mock_scope`) only.

Demo against a local mock Scope: ``uv run python -m seestar_controller.link.device``
"""

from __future__ import annotations

import argparse
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .protocol import Event
from .transport import COMMAND_PORT, EventHandler, LinkTransport

logger = logging.getLogger(__name__)

# -- wire names ---------------------------------------------------------------

#: RPC the facade uses to read the Scope's device state.
GET_SCOPE_STATE_METHOD = "get_scope_state"
#: RPC the facade uses to slew to a target.
GOTO_METHOD = "scope_goto_target"
#: RPC the facade uses to park the Scope.
PARK_METHOD = "scope_park"

#: Pushed event carrying slew progress; ``state`` is "slewing"/"complete".
GOTO_EVENT = "ScopeGoto"
#: Pushed event when the Scope parks.
PARK_EVENT = "ScopePark"
#: ``ScopeGoto`` payload state meaning the slew has arrived.
GOTO_ARRIVED_STATE = "complete"

# -- Scope modes ----------------------------------------------------------------

MODE_IDLE = "idle"
MODE_SLEWING = "slewing"
MODE_STACKING = "stacking"
MODE_PARKED = "parked"


class GotoTimeoutError(TimeoutError):
    """The slew to a target did not report arrival within the goto timeout."""


@dataclass(frozen=True)
class StackingProgress:
    """Stacking progress of the Scope's current stack."""

    state: str
    lapse_ms: int
    frames: int


@dataclass(frozen=True)
class DeviceState:
    """A snapshot of the Scope's device state.

    ``target`` is the current target's ``(ra, dec)`` in hours/degrees, or
    None when no target is set.
    """

    mode: str
    target: tuple[float, float] | None
    stacking: StackingProgress

    @property
    def ra(self) -> float | None:
        return self.target[0] if self.target is not None else None

    @property
    def dec(self) -> float | None:
        return self.target[1] if self.target is not None else None

    @property
    def slewing(self) -> bool:
        return self.mode == MODE_SLEWING

    @property
    def parked(self) -> bool:
        return self.mode == MODE_PARKED


def _parse_state(result: Mapping[str, Any]) -> DeviceState:
    """Parse a ``get_scope_state`` result into a :class:`DeviceState`."""
    mode = str(result.get("mode", "unknown"))
    target_raw = result.get("target")
    target: tuple[float, float] | None = None
    if target_raw:
        target = (float(target_raw["ra"]), float(target_raw["dec"]))
    stacking_raw = result.get("stacking") or {}
    stacking = StackingProgress(
        state=str(stacking_raw.get("state", "idle")),
        lapse_ms=int(stacking_raw.get("lapse_ms", 0)),
        frames=int(stacking_raw.get("frames", 0)),
    )
    return DeviceState(mode=mode, target=target, stacking=stacking)


class Device:
    """High-level facade over a connected Link to the Scope."""

    def __init__(self, transport: LinkTransport, *, goto_timeout: float = 30.0) -> None:
        self._transport = transport
        #: how long :meth:`goto` waits for the arrival event before giving up
        self.goto_timeout = goto_timeout
        self._state_lock = threading.Lock()
        self._last_state: DeviceState | None = None

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def connect(
        cls,
        ip: str,
        port: int = COMMAND_PORT,
        *,
        transport: LinkTransport | None = None,
        goto_timeout: float = 30.0,
        **transport_kwargs: Any,
    ) -> "Device":
        """Connect to the Scope at ``ip`` and return a facade with live state.

        Either pass an existing (connected or not) ``transport``, or the
        transport's own keyword arguments (``pem=``, ``pem_path=``,
        ``private_key=``, timeouts, ...) which are forwarded to
        :class:`LinkTransport`. The signed handshake runs on connect; the
        Scope's state is read once so the facade starts live.
        """
        if transport is not None and transport_kwargs:
            raise ValueError(
                "pass either transport= or LinkTransport keyword arguments, not both"
            )
        if transport is None:
            transport = LinkTransport(ip, port, **transport_kwargs)
        if not transport.connected:
            transport.connect()
        device = cls(transport, goto_timeout=goto_timeout)
        device.state()  # prime the live state
        logger.info("device facade connected to scope at %s:%s", ip, port)
        return device

    def disconnect(self) -> None:
        """Close the Link to the Scope."""
        self._transport.disconnect()

    def __enter__(self) -> "Device":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.disconnect()

    @property
    def transport(self) -> LinkTransport:
        return self._transport

    @property
    def connected(self) -> bool:
        return self._transport.connected

    @property
    def last_state(self) -> DeviceState | None:
        """The most recent state snapshot taken by the facade (None before
        the first :meth:`state` call)."""
        with self._state_lock:
            return self._last_state

    # -- state -----------------------------------------------------------------

    def state(self) -> DeviceState:
        """Read the Scope's live state: mode, current target, stacking progress."""
        result = self._transport.request(GET_SCOPE_STATE_METHOD)
        parsed = _parse_state(result)
        with self._state_lock:
            self._last_state = parsed
        return parsed

    # -- goto / park -------------------------------------------------------------

    def goto(
        self, ra: float, dec: float, *, timeout: float | None = None
    ) -> DeviceState:
        """Slew the Scope to the target at ``ra`` (hours) / ``dec`` (degrees).

        Sends ``scope_goto_target`` and waits for the pushed ``ScopeGoto``
        "complete" event (arrival). If the event does not arrive within
        ``timeout`` (default :attr:`goto_timeout`), the state is polled once
        as a fallback; if the Scope is no longer slewing the slew is treated
        as arrived, otherwise :class:`GotoTimeoutError` is raised. Returns
        the state after arrival.
        """
        ra = float(ra)
        dec = float(dec)
        if not 0.0 <= ra < 24.0:
            raise ValueError(f"ra must be in [0, 24) hours, got {ra}")
        if not -90.0 <= dec <= 90.0:
            raise ValueError(f"dec must be in [-90, 90] degrees, got {dec}")

        arrived = threading.Event()

        def on_goto_event(event: Event) -> None:
            if event.payload.get("state") == GOTO_ARRIVED_STATE:
                arrived.set()

        # Register before sending the request so a fast slew cannot be missed.
        self._transport.add_event_handler(GOTO_EVENT, on_goto_event)
        try:
            self._transport.request(GOTO_METHOD, {"ra": ra, "dec": dec}, timeout=timeout)
            wait = timeout if timeout is not None else self.goto_timeout
            if not arrived.wait(wait):
                # Fallback: state polling in case the arrival event was missed.
                polled = self.state()
                if not polled.slewing:
                    return polled
                raise GotoTimeoutError(
                    f"goto(ra={ra}, dec={dec}) did not arrive within {wait}s"
                )
        finally:
            self._transport.remove_event_handler(GOTO_EVENT, on_goto_event)
        return self.state()

    def park(self) -> DeviceState:
        """Park the Scope (mode -> parked). Returns the state after parking."""
        self._transport.request(PARK_METHOD)
        return self.state()

    # -- events ------------------------------------------------------------------

    def add_event_handler(self, name: str, handler: EventHandler) -> None:
        """Register ``handler`` for pushed events named ``name`` ("*" = all).

        Reuses the transport's event dispatch, so every event the Scope
        pushes reaches the facade's callbacks.
        """
        self._transport.add_event_handler(name, handler)

    def remove_event_handler(self, name: str, handler: EventHandler) -> None:
        self._transport.remove_event_handler(name, handler)

    def on_event(self, name: str, handler: EventHandler | None = None) -> Any:
        """Register ``handler`` for pushed events named ``name`` ("*" = all).

        Returns ``handler``; called with only ``name`` it returns a
        decorator, so ``@device.on_event("Stack")`` works too.
        """
        if handler is None:
            def decorator(fn: EventHandler) -> EventHandler:
                self.add_event_handler(name, fn)
                return fn

            return decorator
        self.add_event_handler(name, handler)
        return handler


def connect(ip: str, port: int = COMMAND_PORT, **kwargs: Any) -> Device:
    """Connect to the Scope at ``ip`` and return a live :class:`Device` facade."""
    return Device.connect(ip, port, **kwargs)


def main(argv: list[str] | None = None) -> None:
    """Demo against a local mock Scope: connect -> state -> goto -> park."""
    parser = argparse.ArgumentParser(
        description="Device facade demo against a local mock Scope"
    )
    parser.add_argument("--slew-duration", type=float, default=0.5)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from . import auth
    from .mock_scope import MockScope

    private_pem, public_pem = auth.generate_keypair()
    scope = MockScope(
        "127.0.0.1", 0, 0, 0, public_pem=public_pem, slew_duration=args.slew_duration
    )
    scope.start()
    try:
        device = connect(
            "127.0.0.1",
            scope.command_port,
            pem=private_pem,
            heartbeat_interval=0.5,
        )
        device.add_event_handler(
            "*", lambda e: print(f"  event: {e.name} {dict(e.payload)}")
        )
        print("state after connect:", device.state())
        print("goto(5.5, 12.25) ->", device.goto(5.5, 12.25))
        print("park() ->", device.park())
        device.disconnect()
    finally:
        scope.stop()


if __name__ == "__main__":
    main()
