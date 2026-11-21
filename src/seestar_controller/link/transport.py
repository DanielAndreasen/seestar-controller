"""Client transport for the Link: signed connect, RPC correlation, events, heartbeat, reconnect.

:class:`LinkTransport` owns the command-channel socket (TCP :4700) to the
Scope: it performs the firmware >= 7.18 PEM-signed handshake on connect,
correlates requests/replies by ``id`` (concurrent requests are supported),
dispatches pushed events to registered handlers, and keeps the connection
alive with the ``test_connection`` heartbeat — liveness is tracked via the
last inbound activity, so only socket EOF or total silence is a lost
connection.

**Resilience** (ticket 07): when the Link drops (EOF, heartbeat silence,
send failure) and a :class:`~seestar_controller.link.resilience.ReconnectPolicy`
was passed as ``reconnect=``, the transport reconnects automatically with
exponential backoff (``base_delay`` doubling per attempt, capped at
``cap``, at most ``max_attempts`` tries), re-runs the signed handshake, and
re-arms the event subscriptions (registered handlers survive the
reconnect). The caller-visible :attr:`connection_state` goes
``connected -> reconnecting -> connected``. Lifecycle/error events
(``connection_lost``, ``reconnecting``, ``reconnected``, ``scope_error``)
are dispatched through the same unified event registry as wire events —
exactly once per occurrence — via :meth:`add_event_handler`.

**Protocol log**: every JSON-RPC message (requests, replies, events) is
written with direction + timestamp to a rotating log file under the config
dir (see :mod:`seestar_controller.link.protocol_log`), toggleable via the
``protocol_log=`` / ``protocol_log_path=`` arguments or the
``SEESTAR_PROTOCOL_LOG`` / ``SEESTAR_PROTOCOL_LOG_PATH`` env vars. Key
material (``sign`` fields, PEM blocks) is redacted before it hits the disk.

The handshake key comes from :mod:`seestar_controller.link.keys`: the
configured PEM key file (explicit ``pem_path`` > ``SEESTAR_KEY_PATH`` env
var > ``~/.config/seestar-controller/key.pem``), or the ``pem`` /
``private_key`` overrides used by tests. A missing or invalid key file
raises :class:`~seestar_controller.link.keys.MissingKeyError` /
:class:`~seestar_controller.link.keys.InvalidKeyError` before any socket is
opened.

NOTE: the signed handshake (``get_verify_str`` -> ``verify_client`` with the
``key_envelope`` result, ``pi_is_verified``, ``test_connection``) was
live-verified against a real Seestar S50 in station mode, firmware 9.75
(2026-11-20); beyond the handshake the transport is exercised against the
mock Scope (:mod:`seestar_controller.link.mock_scope`).
"""

from __future__ import annotations

import itertools
import json
import logging
import socket
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Self

from . import auth, keys, protocol
from .protocol import Event, Reply
from .protocol_log import ProtocolLog, resolve_protocol_log
from .resilience import (
    CONNECTION_LOST_EVENT,
    RECONNECTED_EVENT,
    RECONNECTING_EVENT,
    SCOPE_ERROR_EVENT,
    ConnectionState,
    ReconnectPolicy,
)

logger = logging.getLogger(__name__)

#: Default command-channel port of the Scope.
COMMAND_PORT = 4700

EventHandler = Callable[[Event], None]
ConnectionLostHandler = Callable[[], None]


class RpcError(Exception):
    """The Scope replied with a non-zero ``code``."""

    def __init__(self, reply: Reply) -> None:
        self.reply = reply
        super().__init__(
            f"RPC {reply.method!r} (id={reply.id}) failed with code={reply.code}: "
            f"{reply.result!r}"
        )


class ConnectionLostError(Exception):
    """The Link dropped (socket EOF, protocol error, or dead link)."""


class RequestTimeoutError(TimeoutError):
    """A request got no reply within its own timeout.

    The link itself may still be up (busy but healthy) — this is *not* a
    connection loss and does not affect other pending requests.
    """


class LinkTransport:
    """Client side of the Link's command channel (TCP :4700)."""

    def __init__(
        self,
        host: str,
        port: int = COMMAND_PORT,
        *,
        private_key: Any = None,
        pem: bytes | None = None,
        pem_path: str | Path | None = None,
        heartbeat_interval: float = 3.0,
        heartbeat_timeout: float = 30.0,
        request_timeout: float = 10.0,
        connect_timeout: float = 5.0,
        reconnect: ReconnectPolicy | None = None,
        protocol_log: bool | ProtocolLog | None = None,
        protocol_log_path: str | Path | None = None,
    ) -> None:
        self.host = host
        self.port = port
        #: Resolved PEM key path when the key is loaded from disk (None when
        #: ``private_key``/``pem`` was passed directly).
        self.key_path: Path | None = None
        if private_key is None:
            if pem is not None:
                private_key = auth.load_private_key(pem)
            else:
                # Configured key file: explicit pem_path > SEESTAR_KEY_PATH
                # env var > ~/.config/seestar-controller/key.pem. Missing or
                # invalid keys raise MissingKeyError/InvalidKeyError with an
                # actionable message naming the extraction step.
                self.key_path = keys.resolve_key_path(pem_path)
                private_key = keys.load_key(self.key_path)
        self._private_key = private_key
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.request_timeout = request_timeout
        self.connect_timeout = connect_timeout
        #: Automatic reconnect policy; None = no auto-reconnect (opt-in).
        self._reconnect_policy = reconnect
        #: Rotating protocol log of all JSON-RPC traffic (None = disabled).
        self._protocol_log = resolve_protocol_log(protocol_log, path=protocol_log_path)

        self._sock: socket.socket | None = None
        # RLock: request_raw holds it while calling _send, which re-takes it.
        self._send_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._pending: dict[int, Future[Reply]] = {}
        #: ids of in-flight requests whose 103 (method not found) reply must
        #: not raise a scope_error event — used by the fw-version probes in
        #: :mod:`.compat`, where a 103 is an expected dialect answer.
        self._suppress_103_ids: set[int] = set()
        self._ids = itertools.count(1)
        self._event_handlers: dict[str, list[EventHandler]] = {}
        self._lost_handlers: list[ConnectionLostHandler] = []
        self._reader_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._reconnect_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._reconnect_stop = threading.Event()
        self._connected = False
        self._link_up = False  # socket open and not known-lost (set before handshake)
        self._verified = False
        self._state = ConnectionState.DISCONNECTED
        #: reconnect attempts made in the current/last reconnect cycle
        self._reconnect_attempts = 0
        #: monotonic time of the last inbound bytes — the liveness signal
        self._last_activity: float = 0.0

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        """Open the socket, run the signed handshake, start IO threads."""
        self._stop.clear()
        self._reconnect_stop.clear()
        with self._state_lock:
            self._state = ConnectionState.CONNECTING
        self._open_and_handshake()
        with self._state_lock:
            self._state = ConnectionState.CONNECTED
        logger.info("connected to scope at %s:%s (signed handshake ok)", self.host, self.port)

    def _open_and_handshake(self) -> None:
        """Open the socket, start the reader, run the signed handshake, start
        the heartbeat. Shared by :meth:`connect` and the reconnect loop (which
        re-runs the handshake on every attempt)."""
        sock = socket.create_connection(
            (self.host, self.port), timeout=self.connect_timeout
        )
        sock.settimeout(None)
        self._sock = sock
        self._link_up = True
        self._last_activity = time.monotonic()
        reader = threading.Thread(
            target=self._reader_loop, args=(sock,), name="link-reader", daemon=True
        )
        self._reader_thread = reader
        reader.start()
        # Firmware >= 7.18 (incl. 9.75): signed handshake before anything else.
        try:
            auth.signed_handshake(self.request_raw, self._private_key)
        except BaseException:
            # Handshake failed: tear this attempt down so a failed connect()
            # leaks nothing and the transport stays reusable.
            self._link_up = False
            with self._state_lock:
                self._connected = False
                self._verified = False
            self._close_current_socket()
            if reader.is_alive():
                reader.join(timeout=2.0)
            self._reader_thread = None
            raise
        with self._state_lock:
            self._connected = True
            self._verified = True
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, args=(sock,), name="link-heartbeat", daemon=True
        )
        self._heartbeat_thread.start()

    def disconnect(self) -> None:
        """Close the Link, stop the IO and reconnect threads, close the log.

        Safe to call from any thread, including a connection-lost handler
        running on the reader/heartbeat thread itself (never joins the
        calling thread).
        """
        self._stop.set()
        self._reconnect_stop.set()
        with self._state_lock:
            self._connected = False
            self._state = ConnectionState.DISCONNECTED
        self._link_up = False
        self._close_current_socket()
        self._fail_pending(ConnectionLostError("disconnected"))
        current = threading.current_thread()
        for thread in (self._reader_thread, self._heartbeat_thread, self._reconnect_thread):
            if (
                thread is not None
                and thread is not current
                and thread.is_alive()
            ):
                thread.join(timeout=2.0)
        if self._protocol_log is not None:
            self._protocol_log.close()

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.disconnect()

    @property
    def connected(self) -> bool:
        with self._state_lock:
            return self._connected

    @property
    def verified(self) -> bool:
        """True after the PEM-signed handshake succeeded."""
        with self._state_lock:
            return self._verified

    @property
    def connection_state(self) -> ConnectionState:
        """Caller-visible Link state: disconnected / connecting / connected /
        reconnecting (see :class:`~seestar_controller.link.resilience.ConnectionState`)."""
        with self._state_lock:
            return self._state

    @property
    def reconnect_attempts(self) -> int:
        """Reconnect attempts made in the current/last reconnect cycle."""
        return self._reconnect_attempts

    @property
    def subscriptions(self) -> frozenset[str]:
        """Event names with registered handlers — the client-side
        subscriptions that survive a reconnect (handlers are re-armed
        automatically when the Link comes back)."""
        with self._state_lock:
            return frozenset(name for name in self._event_handlers if name != "*")

    @property
    def protocol_log(self) -> ProtocolLog | None:
        """The rotating protocol log of this transport's JSON-RPC traffic
        (None when disabled)."""
        return self._protocol_log

    # -- RPC ---------------------------------------------------------------

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | Sequence[Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Send one request, wait for its reply, return ``result``.

        ``params`` is normally a mapping (``{"ra": ..., "dec": ...}``), but
        some fw 9.75 methods take POSITIONAL params — ``scope_goto`` expects
        ``[ra_hours, dec_deg]`` (seestarpy ``raw.scope_goto`` sends
        ``'params': [ra, dec]``) — so a list/tuple is passed through to the
        wire unchanged instead of being mangled by ``dict(params)``.

        Raises :class:`RpcError` on a non-zero reply code,
        :class:`RequestTimeoutError` if no reply arrives within ``timeout``
        (the link may still be up), and :class:`ConnectionLostError` if the
        Link drops while waiting.
        """
        reply = self.request_raw(method, params, timeout=timeout)
        if not reply.ok:
            raise RpcError(reply)
        return reply.result

    def request_raw(
        self,
        method: str,
        params: Mapping[str, Any] | Sequence[Any] | None = None,
        *,
        timeout: float | None = None,
        suppress_scope_error: bool = False,
    ) -> Reply:
        """Send one request and return the correlated :class:`Reply`.

        ``params`` may be a mapping (sent as a JSON object) or a list/tuple
        (sent as a JSON array, unchanged — fw 9.75 ``scope_goto`` takes
        positional ``[ra, dec]`` and silently ignores a dict).

        With ``suppress_scope_error=True`` a 103 (method not found) reply is
        still returned (and still resolves the future) but does not dispatch
        a ``scope_error`` lifecycle event — other error codes always do.

        ``timeout`` overrides :attr:`request_timeout` for this call, as given:
        ``timeout=0`` waits no longer than the instant itself (a reply not
        already in raises :class:`RequestTimeoutError` at once), while
        ``timeout=None`` uses the configured default.
        """
        with self._state_lock:
            if not self._link_up:
                raise ConnectionLostError("not connected")
        msg: dict[str, Any] = {"id": next(self._ids), "method": method}
        if params:
            # Positional params (fw 9.75 scope_goto [ra, dec]) go on the wire
            # as a JSON array; mappings as a JSON object.
            msg["params"] = (
                list(params) if isinstance(params, (list, tuple)) else dict(params)
            )
        future: Future[Reply] = Future()
        send_failed = False
        with self._send_lock:
            self._pending[msg["id"]] = future
            if suppress_scope_error:
                self._suppress_103_ids.add(msg["id"])
            try:
                self._send(msg)
            except (OSError, ConnectionLostError):
                self._pending.pop(msg["id"], None)
                self._suppress_103_ids.discard(msg["id"])
                send_failed = True
        if send_failed:
            # Connection-lost handling (incl. user handlers) must run outside
            # _send_lock so handlers can safely call back into the transport.
            self._handle_connection_lost("send failed")
            raise ConnectionLostError("send failed") from None
        # An explicit numeric timeout is honored as-is — including 0 ("no
        # wait"); only None falls back to the configured default.
        effective_timeout = self.request_timeout if timeout is None else timeout
        try:
            return future.result(timeout=effective_timeout)
        except TimeoutError:
            with self._send_lock:
                self._pending.pop(msg["id"], None)
                self._suppress_103_ids.discard(msg["id"])
            raise RequestTimeoutError(
                f"no reply to {method!r} (id={msg['id']}) within "
                f"{effective_timeout}s"
            ) from None

    def _send(self, msg: Mapping[str, Any]) -> None:
        sock = self._sock
        if sock is None:
            raise ConnectionLostError("not connected")
        with self._send_lock:
            sock.sendall(protocol.encode_message(msg))
            self._log_traffic("tx", "request", msg)

    # -- events ------------------------------------------------------------

    def add_event_handler(self, name: str, handler: EventHandler) -> None:
        """Register ``handler`` for pushed events named ``name`` ("*" = all).

        Also accepts the link lifecycle/error event names
        (:data:`~seestar_controller.link.resilience.LIFECYCLE_EVENTS`:
        ``connection_lost``, ``reconnecting``, ``reconnected``,
        ``scope_error``, ``stack_finished``) — each dispatched exactly once
        per occurrence.
        """
        with self._state_lock:
            self._event_handlers.setdefault(name, []).append(handler)

    def remove_event_handler(self, name: str, handler: EventHandler) -> None:
        with self._state_lock:
            handlers = self._event_handlers.get(name, [])
            if handler in handlers:
                handlers.remove(handler)

    def add_connection_lost_handler(self, handler: ConnectionLostHandler) -> None:
        """Register a plain callback fired once when the Link drops (in
        addition to the ``connection_lost`` event dispatch)."""
        with self._state_lock:
            self._lost_handlers.append(handler)

    def dispatch_lifecycle(
        self, name: str, payload: Mapping[str, Any] | None = None
    ) -> None:
        """Dispatch a link lifecycle/error event to the handlers registered
        for ``name`` (and "*"), exactly once per call.

        Used internally for ``connection_lost`` / ``reconnecting`` /
        ``reconnected`` / ``scope_error``; the device facade uses it for
        ``stack_finished``.
        """
        event = Event(
            name=name, timestamp=f"{time.time():.9f}", payload=dict(payload or {})
        )
        self._log_traffic("local", "lifecycle", {"Event": name, **event.payload})
        self._dispatch_event(event)

    # -- internals ---------------------------------------------------------

    def _log_traffic(self, direction: str, kind: str, message: Mapping[str, Any]) -> None:
        """Write one traffic record to the protocol log (if enabled)."""
        log = self._protocol_log
        if log is None:
            return
        try:
            log.log(direction, kind, message)
        except Exception:
            logger.exception("protocol log write failed")

    def _reader_loop(self, sock: socket.socket) -> None:
        decoder = protocol.MessageDecoder()
        # The loop is bound to *this* socket: a reconnect replaces self._sock,
        # and a stale reader must neither read the new socket nor report a
        # loss for a connection it no longer owns.
        while not self._stop.is_set() and self._sock is sock:
            try:
                data = sock.recv(65536)
            except OSError:
                data = b""
            if not data:
                # Socket EOF is the only *immediate* connection-loss signal.
                self._handle_connection_lost("socket eof", sock)
                break
            # Any inbound bytes count as liveness activity.
            self._last_activity = time.monotonic()
            try:
                messages = decoder.feed(data)
            except json.JSONDecodeError as exc:
                # Protocol error: one malformed line must not kill the reader
                # thread while `connected` stays True — treat it as a lost
                # connection.
                logger.warning("protocol error: malformed JSON line from scope: %s", exc)
                self._handle_connection_lost("protocol error", sock)
                break
            for message in messages:
                self._dispatch(message)

    def _dispatch(self, message: Mapping[str, Any]) -> None:
        decoded = protocol.classify(message)
        if isinstance(decoded, Reply):
            self._log_traffic("rx", "reply", message)
            with self._send_lock:
                future = self._pending.pop(decoded.id, None)
            if future is not None and not future.done():
                future.set_result(decoded)
            if not decoded.ok:
                # The Scope reported an error: dispatch it once per error reply.
                # Exception: a 103 on a request flagged suppress_scope_error
                # (fw-dialect probe in .compat) is an expected answer, not an
                # error worth surfacing to lifecycle listeners.
                with self._send_lock:
                    suppress = (
                        decoded.code == 103 and decoded.id in self._suppress_103_ids
                    )
                    self._suppress_103_ids.discard(decoded.id)
                if not suppress:
                    self.dispatch_lifecycle(
                        SCOPE_ERROR_EVENT,
                        {
                            "id": decoded.id,
                            "method": decoded.method,
                            "code": decoded.code,
                            "result": decoded.result,
                        },
                    )
        elif isinstance(decoded, Event):
            self._log_traffic("rx", "event", message)
            self._dispatch_event(decoded)

    def _dispatch_event(self, event: Event) -> None:
        with self._state_lock:
            handlers = list(self._event_handlers.get(event.name, []))
            handlers += list(self._event_handlers.get("*", []))
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                logger.exception("event handler for %r failed", event.name)

    def _heartbeat_loop(self, sock: socket.socket) -> None:
        """Send ``test_connection`` every interval; detect a dead link.

        Liveness is based on *last inbound activity* (any message, not just
        heartbeat replies): a busy-but-healthy link whose heartbeat replies
        are slow must not be declared lost, and a heartbeat timeout must not
        fail other pending requests. Only socket EOF (handled by the reader)
        or total inbound silence longer than ``heartbeat_timeout`` counts as
        a lost link. ``heartbeat_timeout`` is deliberately >> ``request_timeout``
        so slow-but-answering requests never trip the heartbeat.
        """
        while not self._stop.is_set() and self.connected and self._sock is sock:
            self._stop.wait(self.heartbeat_interval)
            if self._stop.is_set() or not self.connected or self._sock is not sock:
                break
            try:
                self.request_raw(
                    protocol.HEARTBEAT_METHOD, timeout=self.heartbeat_timeout
                )
            except RequestTimeoutError:
                # Heartbeat reply is late; the link may still be busy. Only
                # declare the link lost if *nothing* came in for the whole
                # heartbeat timeout.
                idle = time.monotonic() - self._last_activity
                if idle > self.heartbeat_timeout:
                    logger.warning(
                        "no inbound activity for %.1fs (heartbeat timeout %.1fs) — link lost",
                        idle,
                        self.heartbeat_timeout,
                    )
                    self._handle_connection_lost("heartbeat timeout", sock)
                    return
                logger.debug(
                    "heartbeat reply late (%.1fs idle); link still active", idle
                )
            except (ConnectionLostError, RpcError, OSError) as exc:
                logger.warning("heartbeat failed: %s", exc)
                self._handle_connection_lost(f"heartbeat failed: {exc}", sock)
                return

    # -- connection loss + reconnect ----------------------------------------

    def _handle_connection_lost(
        self, reason: str = "unknown", sock: socket.socket | None = None
    ) -> None:
        """Mark the Link lost, fail pending requests, dispatch the
        ``connection_lost`` event exactly once, and (with a reconnect policy)
        start the reconnect loop.

        ``sock`` optionally identifies the connection the report is about; a
        stale report from an already-replaced connection is ignored. Runs no
        locks while invoking user handlers: callers must not hold _send_lock
        here (handlers may call back into the transport, e.g. disconnect()).
        """
        with self._state_lock:
            if sock is not None and self._sock is not sock:
                return  # stale report from a replaced connection
            was_connected = self._connected
            self._connected = False
            self._link_up = False
            handlers = list(self._lost_handlers)
            start_reconnect = (
                was_connected
                and self._reconnect_policy is not None
                and not self._stop.is_set()
                and not self._reconnect_stop.is_set()
            )
            if was_connected:
                self._state = (
                    ConnectionState.RECONNECTING
                    if start_reconnect
                    else ConnectionState.DISCONNECTED
                )
        # Close the dead socket: this also wakes a reader blocked in recv().
        self._close_current_socket()
        self._fail_pending(ConnectionLostError("connection lost"))
        if not was_connected:
            return
        logger.warning("link to %s:%s lost (%s)", self.host, self.port, reason)
        self.dispatch_lifecycle(CONNECTION_LOST_EVENT, {"reason": reason})
        for handler in handlers:
            try:
                handler()
            except Exception:
                logger.exception("connection-lost handler failed")
        if start_reconnect:
            self._start_reconnect_loop(reason)

    def _close_current_socket(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _start_reconnect_loop(self, reason: str) -> None:
        if self._stop.is_set() or self._reconnect_stop.is_set():
            return
        with self._state_lock:
            existing = self._reconnect_thread
            if existing is not None and existing.is_alive():
                return
            self._reconnect_attempts = 0
            thread = threading.Thread(
                target=self._reconnect_loop,
                args=(reason,),
                name="link-reconnect",
                daemon=True,
            )
            self._reconnect_thread = thread
        thread.start()

    def _reconnect_loop(self, reason: str) -> None:
        """Reconnect with exponential backoff, re-running the signed handshake
        on every attempt, until success, the attempt bound, or disconnect()."""
        policy = self._reconnect_policy
        if policy is None:
            return
        if self._stop.is_set() or self._reconnect_stop.is_set():
            return
        self.dispatch_lifecycle(
            RECONNECTING_EVENT,
            {
                "reason": reason,
                "max_attempts": policy.max_attempts,
                "base_delay": policy.base_delay,
                "cap": policy.cap,
            },
        )
        attempt = 0
        while not self._stop.is_set() and not self._reconnect_stop.is_set():
            if policy.max_attempts is not None and attempt >= policy.max_attempts:
                logger.warning(
                    "reconnect to %s:%s: giving up after %d attempt(s)",
                    self.host,
                    self.port,
                    policy.max_attempts,
                )
                with self._state_lock:
                    if not self._connected:
                        self._state = ConnectionState.DISCONNECTED
                return
            attempt += 1
            self._reconnect_attempts = attempt
            delay = policy.delay_for(attempt)
            logger.info(
                "reconnect attempt %d to %s:%s in %.2fs", attempt, self.host, self.port, delay
            )
            if self._reconnect_stop.wait(delay) or self._stop.is_set():
                return
            try:
                self._open_and_handshake()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "reconnect attempt %d to %s:%s failed: %s",
                    attempt,
                    self.host,
                    self.port,
                    exc,
                )
                continue
            if self._stop.is_set() or self._reconnect_stop.is_set():
                # disconnect() raced this attempt: tear the fresh link down.
                with self._state_lock:
                    self._connected = False
                    self._link_up = False
                    self._state = ConnectionState.DISCONNECTED
                self._close_current_socket()
                self._fail_pending(ConnectionLostError("disconnected"))
                return
            with self._state_lock:
                self._state = ConnectionState.CONNECTED
            logger.info(
                "reconnected to %s:%s after %d attempt(s)", self.host, self.port, attempt
            )
            self.dispatch_lifecycle(
                RECONNECTED_EVENT, {"attempts": attempt, "reason": reason}
            )
            return

    def _fail_pending(self, exc: Exception) -> None:
        with self._send_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(exc)
