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
- :meth:`Device.start_stack` / :meth:`Device.stop_stack` — stack sub-frames
  into a Stack on the current target (``iscope_start_stacking`` /
  ``iscope_stop_stacking``) with per-target exposure / gain / mosaic
  framing; progress arrives as pushed ``Stack`` events;
- :meth:`Device.park` — park the Scope (``scope_park``, mode -> parked);
- :meth:`Device.download_frames` — download **sub-frames** or **Stacks**
  (GLOSSARY) into a caller-given directory as ``.fits`` + ``.json`` metadata
  sidecar, written atomically (``list_frames`` / ``get_frame`` RPCs, see
  :mod:`seestar_controller.link.frames`).

Pushed events from the Scope are surfaced through registered callbacks
(:meth:`Device.add_event_handler` / :meth:`Device.on_event`), reusing the
transport's event dispatch. Stack progress events (``Stack``) can also be
received pre-parsed as :class:`StackingProgress` via
:meth:`Device.add_stacking_progress_handler` / :meth:`Device.on_stacking_progress`,
and are reflected in :meth:`Device.state` (``state().stacking``).

**Resilience** (ticket 07): :meth:`Device.connect` enables automatic
reconnect with exponential backoff by default (pass ``reconnect=None`` to
disable, or a :class:`~seestar_controller.link.resilience.ReconnectPolicy`
to tune ``max_attempts`` / ``base_delay`` / ``cap``). When the Link drops,
the transport reconnects, re-runs the signed handshake, and re-arms the
event subscriptions; :attr:`Device.connection_state` surfaces
``connected`` / ``reconnecting`` / ``disconnected``. Link lifecycle and
error events — ``connection_lost``, ``reconnecting``, ``reconnected``,
``scope_error``, ``stack_finished`` — are dispatched through the same
:meth:`Device.add_event_handler` registry as wire events, exactly once per
occurrence::

    device.add_event_handler("connection_lost", lambda e: print("link lost:", e.payload))
    device.add_event_handler("reconnected", lambda e: print("back after", e.payload["attempts"], "attempts"))
    device.add_event_handler("stack_finished", lambda e: print("stack done:", e.payload["frames"], "sub-frames"))

All JSON-RPC traffic is written to a rotating protocol log under the config
dir (toggleable via ``protocol_log=`` / ``protocol_log_path=`` or the
``SEESTAR_PROTOCOL_LOG`` env var; signatures/PEM are redacted — see
:mod:`seestar_controller.link.protocol_log`).

NOTE (user AFK, no real scope / no PEM key — docs/wiki/assumptions.md):
the facade is exercised against the mock Scope
(:mod:`seestar_controller.link.mock_scope`) only.

Demo against a local mock Scope: ``uv run python -m seestar_controller.link.device``
"""

from __future__ import annotations

import argparse
import logging
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from . import frames, stacking
from .protocol import Event
from .resilience import (
    STACK_FINISHED_EVENT,
    ConnectionState,
    ReconnectPolicy,
)
from .stacking import START_STACK_METHOD, STACK_EVENT, STACK_STATE_IDLE, STOP_STACK_METHOD
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
    """Stacking progress of the Scope's current stack.

    ``frames`` is the number of sub-frames stacked so far, ``lapse_ms`` the
    elapsed stacking time in milliseconds, ``state`` "working" while the
    Stack is being collected and "idle" once it stopped.
    """

    state: str
    lapse_ms: int
    frames: int


def _stacking_progress_from(payload: Mapping[str, Any]) -> StackingProgress:
    """Parse a ``Stack`` event payload / ``get_scope_state`` stacking dict."""
    return StackingProgress(
        state=str(payload.get("state", "idle")),
        lapse_ms=int(payload.get("lapse_ms", 0) or 0),
        frames=int(payload.get("frames", 0) or 0),
    )


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
    def stacking_active(self) -> bool:
        return self.mode == MODE_STACKING

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
    stacking = _stacking_progress_from(result.get("stacking") or {})
    return DeviceState(mode=mode, target=target, stacking=stacking)


class Device:
    """High-level facade over a connected Link to the Scope."""

    def __init__(self, transport: LinkTransport, *, goto_timeout: float = 30.0) -> None:
        self._transport = transport
        #: how long :meth:`goto` waits for the arrival event before giving up
        self.goto_timeout = goto_timeout
        self._state_lock = threading.Lock()
        self._last_state: DeviceState | None = None
        #: StackingProgress handler -> registered raw Stack event wrapper
        self._progress_handlers: dict[Callable[[StackingProgress], None], EventHandler] = {}
        # Translate the final Stack event (state "idle") into the unified
        # "stack_finished" lifecycle event, dispatched through the transport.
        transport.add_event_handler(STACK_EVENT, self._on_stack_event)

    def _on_stack_event(self, event: Event) -> None:
        if str(event.payload.get("state", "")) == STACK_STATE_IDLE:
            self._transport.dispatch_lifecycle(
                STACK_FINISHED_EVENT,
                {
                    "state": STACK_STATE_IDLE,
                    "lapse_ms": int(event.payload.get("lapse_ms", 0) or 0),
                    "frames": int(event.payload.get("frames", 0) or 0),
                },
            )

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

        Unless a ``transport`` is passed, the facade enables automatic
        reconnect with a default
        :class:`~seestar_controller.link.resilience.ReconnectPolicy`
        (exponential backoff, unlimited attempts) — pass ``reconnect=None``
        to disable or a custom policy to tune it. On a dropped Link the
        transport reconnects, re-runs the handshake, and re-arms the event
        subscriptions; see :attr:`connection_state`.
        """
        if transport is not None and transport_kwargs:
            raise ValueError(
                "pass either transport= or LinkTransport keyword arguments, not both"
            )
        if transport is None:
            # Resilient by default at the facade level (spec user story 12:
            # a WiFi blip must not kill the session).
            transport_kwargs.setdefault("reconnect", ReconnectPolicy())
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
    def connection_state(self) -> ConnectionState:
        """Link state surfaced to callers: ``connected`` / ``reconnecting`` /
        ``disconnected`` / ``connecting``."""
        return self._transport.connection_state

    @property
    def reconnect_attempts(self) -> int:
        """Reconnect attempts made in the current/last reconnect cycle."""
        return self._transport.reconnect_attempts

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

    # -- stacking --------------------------------------------------------------

    def start_stack(
        self,
        exposure: float,
        gain: int,
        mosaic: bool | Mapping[str, float] | tuple[float, float] = False,
        *,
        timeout: float | None = None,
    ) -> DeviceState:
        """Begin stacking a Stack of sub-frames on the current target.

        ``exposure`` is the sub-frame exposure in seconds, ``gain`` the
        sensor gain, and ``mosaic`` the mosaic framing for the target: a
        bool (Scope default geometry) or a ``(pan, tilt)`` framing in
        degrees (mapping with ``pan``/``tilt`` keys works too). Out-of-range
        values raise :class:`ValueError` client-side and are also rejected
        by the Scope with a non-zero reply code (:class:`RpcError`).

        Returns once stacking has begun (mode ``stacking``); progress is
        delivered as pushed ``Stack`` events (see
        :meth:`add_stacking_progress_handler`) and reflected in
        :meth:`state` (``state().stacking``).
        """
        params = stacking.stack_request_params(exposure, gain, mosaic)
        self._transport.request(START_STACK_METHOD, params, timeout=timeout)
        return self.state()

    def stop_stack(self, *, timeout: float | None = None) -> DeviceState:
        """Stop the current Stack cleanly (mode -> idle).

        The Scope pushes a final ``Stack`` progress event with the final
        frame count and elapsed time. Returns the state after stopping.
        """
        self._transport.request(STOP_STACK_METHOD, timeout=timeout)
        return self.state()

    # -- frame downloads -----------------------------------------------------

    def download_frames(
        self,
        dest_dir: str | Path,
        kind: str = frames.KIND_SUB,
        *,
        since: datetime | str | float | None = None,
        timeout: float | None = None,
    ) -> list[frames.DownloadedFrame]:
        """Download sub-frames or Stacks from the Scope into ``dest_dir``.

        ``kind`` is ``"sub"`` (sub-frames, GLOSSARY) or ``"stack"`` (Stacks);
        ``since`` optionally limits the download to frames taken at/after
        that time — a :class:`~datetime.datetime`, an ISO-8601 string, or
        epoch seconds. Each frame is written as ``<id>.fits`` plus a
        ``<id>.json`` metadata sidecar (time, exposure, gain, target), both
        atomically (temp file + rename), so an interrupted download never
        leaves a partial file. Returns the downloaded frames (path, sidecar,
        metadata) in time order. See :mod:`seestar_controller.link.frames`.
        """
        return frames.download_frames(
            self._transport, dest_dir, kind=kind, since=since, timeout=timeout
        )

    def add_stacking_progress_handler(
        self, handler: Callable[[StackingProgress], None]
    ) -> None:
        """Register ``handler`` to receive a parsed :class:`StackingProgress`
        (frames stacked, elapsed) for every pushed ``Stack`` progress event."""

        def on_stack_event(event: Event) -> None:
            handler(_stacking_progress_from(event.payload))

        self._progress_handlers[handler] = on_stack_event
        self._transport.add_event_handler(STACK_EVENT, on_stack_event)

    def remove_stacking_progress_handler(
        self, handler: Callable[[StackingProgress], None]
    ) -> None:
        """Remove a handler registered with :meth:`add_stacking_progress_handler`."""
        on_stack_event = self._progress_handlers.pop(handler, None)
        if on_stack_event is not None:
            self._transport.remove_event_handler(STACK_EVENT, on_stack_event)

    def on_stacking_progress(
        self, handler: Callable[[StackingProgress], None] | None = None
    ) -> Any:
        """Register ``handler`` for stack progress, parsed as
        :class:`StackingProgress`.

        Returns ``handler``; called with only the decorator form, so
        ``@device.on_stacking_progress`` works too.
        """
        if handler is None:
            def decorator(fn: Callable[[StackingProgress], None]) -> Callable[[StackingProgress], None]:
                self.add_stacking_progress_handler(fn)
                return fn

            return decorator
        self.add_stacking_progress_handler(handler)
        return handler

    # -- events ------------------------------------------------------------------

    def add_event_handler(self, name: str, handler: EventHandler) -> None:
        """Register ``handler`` for pushed events named ``name`` ("*" = all).

        Reuses the transport's unified event dispatch, so every event the
        Scope pushes — and every link lifecycle/error event
        (``connection_lost``, ``reconnecting``, ``reconnected``,
        ``scope_error``, ``stack_finished``) — reaches the facade's
        callbacks, exactly once per occurrence.
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
    """Demo against a local mock Scope: connect -> state -> goto -> stack ->
    download frames -> drop the link (reconnect demo) -> park."""
    parser = argparse.ArgumentParser(
        description="Device facade demo against a local mock Scope"
    )
    parser.add_argument("--slew-duration", type=float, default=0.5)
    parser.add_argument("--stack-watch", type=float, default=1.2,
                        help="seconds to watch stack progress before stopping")
    parser.add_argument(
        "--protocol-log",
        type=Path,
        default=None,
        help="protocol log file (default: $SEESTAR_PROTOCOL_LOG_PATH, else "
             "~/.config/seestar-controller/protocol.log; SEESTAR_PROTOCOL_LOG=0 disables)",
    )
    parser.add_argument(
        "--no-reconnect-demo",
        action="store_true",
        help="skip the drop-connection/reconnect demo",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from . import auth
    from .mock_scope import MockScope
    from .resilience import LIFECYCLE_EVENTS

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
            protocol_log_path=args.protocol_log,
        )
        device.add_event_handler(
            "*", lambda e: print(f"  event: {e.name} {dict(e.payload)}")
        )
        for name in LIFECYCLE_EVENTS:
            device.add_event_handler(
                name, lambda e: print(f"  lifecycle: {e.name} {dict(e.payload)}")
            )

        @device.on_stacking_progress
        def on_progress(p: StackingProgress) -> None:
            print(f"  stack progress: {p.frames} sub-frames, {p.lapse_ms} ms elapsed ({p.state})")

        print("state after connect:", device.state())
        print("goto(5.5, 12.25) ->", device.goto(5.5, 12.25))
        print("start_stack(exposure=10s, gain=80, mosaic=(2.0, 2.0)) ->",
              device.start_stack(10.0, 80, (2.0, 2.0)))
        time.sleep(args.stack_watch)
        print("stop_stack() ->", device.stop_stack())
        with tempfile.TemporaryDirectory(prefix="seestar-frames-") as dest:
            for kind in (frames.KIND_SUB, frames.KIND_STACK):
                downloaded = device.download_frames(dest, kind=kind)
                print(f"download_frames(kind={kind!r}) -> {len(downloaded)} frame(s)")
                for d in downloaded:
                    print(
                        f"  {d.meta.id}: {d.path.name} + {d.sidecar.name} "
                        f"(time={d.meta.time} exposure={d.meta.exposure}s "
                        f"gain={d.meta.gain} target={d.meta.target})"
                    )
        if not args.no_reconnect_demo:
            # Kill the link mid-session: the mock drops the command socket
            # without heartbeat; the client must detect the loss, reconnect
            # with backoff, re-run the signed handshake, and keep its
            # subscriptions (the gap is visible in the protocol log).
            print("dropping the link (mock scope closes the socket)...")
            reconnected = threading.Event()
            device.add_event_handler("reconnected", lambda e: reconnected.set())
            scope.drop_connections()
            if reconnected.wait(timeout=15):
                print(f"reconnected (state={device.connection_state}, "
                      f"attempts={device.reconnect_attempts}) -> state:", device.state())
            else:
                print("reconnect did not happen within 15s")
        print("park() ->", device.park())
        device.disconnect()
    finally:
        scope.stop()


if __name__ == "__main__":
    main()
