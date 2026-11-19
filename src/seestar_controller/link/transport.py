"""Client transport for the Link: signed connect, RPC correlation, events, heartbeat.

:class:`LinkTransport` owns the command-channel socket (TCP :4700) to the
Scope: it performs the firmware >= 7.18 PEM-signed handshake on connect,
correlates requests/replies by ``id`` (concurrent requests are supported),
dispatches pushed events to registered handlers, and keeps the connection
alive with the ``test_connection`` heartbeat — liveness is tracked via the
last inbound activity, so only socket EOF or total silence is a lost
connection.

NOTE (user AFK, no real scope / no PEM key — docs/wiki/assumptions.md):
this transport is exercised against the mock Scope
(:mod:`seestar_controller.link.mock_scope`) only.
"""

from __future__ import annotations

import itertools
import json
import logging
import socket
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Mapping

from . import auth, protocol
from .protocol import Event, Reply

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
    ) -> None:
        self.host = host
        self.port = port
        if private_key is None:
            pem_bytes = pem if pem is not None else Path(
                pem_path or auth.DEFAULT_KEY_PATH
            ).read_bytes()
            private_key = auth.load_private_key(pem_bytes)
        self._private_key = private_key
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.request_timeout = request_timeout
        self.connect_timeout = connect_timeout

        self._sock: socket.socket | None = None
        # RLock: request_raw holds it while calling _send, which re-takes it.
        self._send_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._pending: dict[int, Future[Reply]] = {}
        self._ids = itertools.count(1)
        self._event_handlers: dict[str, list[EventHandler]] = {}
        self._lost_handlers: list[ConnectionLostHandler] = []
        self._reader_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = False
        self._link_up = False  # socket open and not known-lost (set before handshake)
        self._verified = False
        #: monotonic time of the last inbound bytes — the liveness signal
        self._last_activity: float = 0.0

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        """Open the socket, run the signed handshake, start IO threads."""
        sock = socket.create_connection(
            (self.host, self.port), timeout=self.connect_timeout
        )
        sock.settimeout(None)
        self._sock = sock
        self._link_up = True
        self._stop.clear()
        self._last_activity = time.monotonic()
        reader = threading.Thread(
            target=self._reader_loop, name="link-reader", daemon=True
        )
        self._reader_thread = reader
        reader.start()
        # Firmware >= 7.18 (incl. 9.75): signed handshake before anything else.
        try:
            auth.signed_handshake(self.request_raw, self._private_key)
        except BaseException:
            # Handshake failed: tear the socket and reader thread down so a
            # failed connect() leaks nothing and the transport stays reusable.
            self._stop.set()
            self._link_up = False
            with self._state_lock:
                self._connected = False
                self._verified = False
            self._sock = None
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
            if reader.is_alive():
                reader.join(timeout=2.0)
            self._reader_thread = None
            raise
        with self._state_lock:
            self._connected = True
            self._verified = True
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="link-heartbeat", daemon=True
        )
        self._heartbeat_thread.start()
        logger.info("connected to scope at %s:%s (signed handshake ok)", self.host, self.port)

    def disconnect(self) -> None:
        """Close the Link and stop the IO threads.

        Safe to call from any thread, including a connection-lost handler
        running on the reader/heartbeat thread itself (never joins the
        calling thread).
        """
        self._stop.set()
        with self._state_lock:
            self._connected = False
        self._link_up = False
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        self._fail_pending(ConnectionLostError("disconnected"))
        current = threading.current_thread()
        for thread in (self._reader_thread, self._heartbeat_thread):
            if (
                thread is not None
                and thread is not current
                and thread.is_alive()
            ):
                thread.join(timeout=2.0)

    def __enter__(self) -> "LinkTransport":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
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

    # -- RPC ---------------------------------------------------------------

    def request(
        self, method: str, params: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any:
        """Send one request, wait for its reply, return ``result``.

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
        self, method: str, params: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> Reply:
        """Send one request and return the correlated :class:`Reply`."""
        with self._state_lock:
            if not self._link_up:
                raise ConnectionLostError("not connected")
        msg: dict[str, Any] = {"id": next(self._ids), "method": method}
        if params:
            msg["params"] = dict(params)
        future: Future[Reply] = Future()
        send_failed = False
        with self._send_lock:
            self._pending[msg["id"]] = future
            try:
                self._send(msg)
            except OSError:
                self._pending.pop(msg["id"], None)
                send_failed = True
        if send_failed:
            # Connection-lost handling (incl. user handlers) must run outside
            # _send_lock so handlers can safely call back into the transport.
            self._handle_connection_lost()
            raise ConnectionLostError("send failed") from None
        try:
            return future.result(timeout=timeout or self.request_timeout)
        except TimeoutError:
            self._pending.pop(msg["id"], None)
            raise RequestTimeoutError(
                f"no reply to {method!r} (id={msg['id']}) within "
                f"{timeout or self.request_timeout}s"
            ) from None

    def _send(self, msg: Mapping[str, Any]) -> None:
        sock = self._sock
        if sock is None:
            raise ConnectionLostError("not connected")
        with self._send_lock:
            sock.sendall(protocol.encode_message(msg))

    # -- events ------------------------------------------------------------

    def add_event_handler(self, name: str, handler: EventHandler) -> None:
        """Register ``handler`` for pushed events named ``name`` ("*" = all)."""
        with self._state_lock:
            self._event_handlers.setdefault(name, []).append(handler)

    def remove_event_handler(self, name: str, handler: EventHandler) -> None:
        with self._state_lock:
            handlers = self._event_handlers.get(name, [])
            if handler in handlers:
                handlers.remove(handler)

    def add_connection_lost_handler(self, handler: ConnectionLostHandler) -> None:
        with self._state_lock:
            self._lost_handlers.append(handler)

    # -- internals ---------------------------------------------------------

    def _reader_loop(self) -> None:
        decoder = protocol.MessageDecoder()
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                break
            try:
                data = sock.recv(65536)
            except OSError:
                data = b""
            if not data:
                # Socket EOF is the only *immediate* connection-loss signal.
                self._handle_connection_lost()
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
                self._handle_connection_lost()
                break
            for message in messages:
                self._dispatch(message)

    def _dispatch(self, message: Mapping[str, Any]) -> None:
        decoded = protocol.classify(message)
        if isinstance(decoded, Reply):
            with self._send_lock:
                future = self._pending.pop(decoded.id, None)
            if future is not None and not future.done():
                future.set_result(decoded)
        elif isinstance(decoded, Event):
            with self._state_lock:
                handlers = list(self._event_handlers.get(decoded.name, []))
                handlers += list(self._event_handlers.get("*", []))
            for handler in handlers:
                try:
                    handler(decoded)
                except Exception:
                    logger.exception("event handler for %r failed", decoded.name)

    def _heartbeat_loop(self) -> None:
        """Send ``test_connection`` every interval; detect a dead link.

        Liveness is based on *last inbound activity* (any message, not just
        heartbeat replies): a busy-but-healthy link whose heartbeat replies
        are slow must not be declared lost, and a heartbeat timeout must not
        fail other pending requests. Only socket EOF (handled by the reader)
        or total inbound silence longer than ``heartbeat_timeout`` counts as
        a lost link. ``heartbeat_timeout`` is deliberately >> ``request_timeout``
        so slow-but-answering requests never trip the heartbeat.
        """
        while not self._stop.is_set() and self.connected:
            self._stop.wait(self.heartbeat_interval)
            if self._stop.is_set() or not self.connected:
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
                    self._handle_connection_lost()
                    return
                logger.debug(
                    "heartbeat reply late (%.1fs idle); link still active", idle
                )
            except (ConnectionLostError, RpcError, OSError) as exc:
                logger.warning("heartbeat failed: %s", exc)
                self._handle_connection_lost()
                return

    def _handle_connection_lost(self) -> None:
        # Runs no locks while invoking user handlers: callers must not hold
        # _send_lock here (handlers may call back into the transport, e.g.
        # disconnect()).
        with self._state_lock:
            was_connected = self._connected
            self._connected = False
            self._link_up = False
            handlers = list(self._lost_handlers)
        self._fail_pending(ConnectionLostError("connection lost"))
        if was_connected:
            logger.warning("link to %s:%s lost", self.host, self.port)
            for handler in handlers:
                try:
                    handler()
                except Exception:
                    logger.exception("connection-lost handler failed")

    def _fail_pending(self, exc: Exception) -> None:
        with self._send_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(exc)
