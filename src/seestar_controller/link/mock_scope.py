"""Mock Scope: a test double speaking the real Seestar wire protocol.

Implements the Link's command channel (JSON-RPC-style messages on TCP
:4700 — requests with id/method/params, replies correlated by id, pushed
events, ``test_connection`` heartbeat) and the image sockets :4800 (tele)
and :4804 (wide) streaming binary frames. Message shapes follow the
open-source seestar_alp / seestarpy projects.

Firmware 9.75 requires the signed handshake, so the mock *verifies* the
client's PEM-signed handshake (``get_verify_str`` challenge ->
``verify_client`` with an RSA-SHA1 PKCS#1 v1.5 signature) against a public
key, and replies 9.75-style with a ``key_envelope`` result.

NOTE (user AFK, no real scope / no PEM key — docs/wiki/assumptions.md):
this mock is the only counterpart the client is tested against. Image
payloads are synthetic (``MOCKFRAME`` + counter), not real JPEGs; only the
20-byte frame framing is exercised.

Run standalone: ``uv run python -m seestar_controller.link.mock_scope``
"""

from __future__ import annotations

import argparse
import logging
import secrets
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from . import auth, image, protocol

logger = logging.getLogger(__name__)

#: Magic prefix of the mock's synthetic image payloads (real scope: JPEG).
FRAME_MAGIC = b"MOCKFRAME"

#: Reply code used when a command arrives before the signed handshake.
NOT_VERIFIED_CODE = 401


def make_frame_payload(seq: int, size: int) -> bytes:
    """Deterministic synthetic payload for frame ``seq``."""
    payload = bytearray(FRAME_MAGIC + struct.pack(">I", seq))
    payload.extend(b"\x00" * max(0, size - len(payload)))
    return bytes(payload[:size])


class _CommandConnection:
    """One client on the command channel (:4700)."""

    def __init__(self, scope: "MockScope", sock: socket.socket, address: Any) -> None:
        self.scope = scope
        self.sock = sock
        self.address = address
        self.challenge: str | None = None
        self.verified = False
        self.write_lock = threading.Lock()

    def send(self, message: Mapping[str, Any]) -> None:
        with self.write_lock:
            self.sock.sendall(protocol.encode_message(message))

    def reply(self, request_id: int, method: str, result: Any, code: int = 0) -> None:
        self.send(
            {
                "jsonrpc": "2.0",
                "Timestamp": f"{time.time():.9f}",
                "method": method,
                "result": result,
                "code": code,
                "id": request_id,
            }
        )

    def handle(self, request: protocol.Request) -> None:
        method = request.method
        params = dict(request.params)

        delay = self.scope.reply_delay.get(method)
        if delay:
            time.sleep(delay)

        if method == auth.AUTH_CHALLENGE_METHOD:
            self.challenge = secrets.token_hex(24)
            self.reply(request.id, method, {"str": self.challenge})
            return

        if method == auth.AUTH_VERIFY_METHOD:
            sign = str(params.get("sign", ""))
            data = str(params.get("data", ""))
            ok = (
                self.scope.public_key is not None
                and data == self.challenge
                and auth.verify_challenge(self.scope.public_key, data, sign)
            )
            if ok:
                self.verified = True
                # Firmware 9.75 style: success signalled by code == 0 with a
                # key_envelope result instead of result == 0.
                self.reply(
                    request.id,
                    method,
                    {"key_envelope": {"alg": "RSA-OAEP", "wrapped": "mock"}},
                    code=0,
                )
            else:
                self.reply(request.id, method, {"error": "bad signature"}, code=-1)
            return

        if method == auth.AUTH_STATUS_METHOD:
            self.reply(request.id, method, self.verified)
            return

        if method == protocol.HEARTBEAT_METHOD:
            if self.scope.heartbeat_silent:
                # Simulate a scope that stopped answering heartbeats.
                return
            with self.scope._stats_lock:
                self.scope.heartbeat_count += 1
            self.reply(request.id, method, {"status": "ok"})
            return

        if method == "mock_push_event":
            self.scope.push_event(params)
            self.reply(request.id, method, {"pushed": True})
            return

        if method == "mock_fail":
            self.reply(request.id, method, {"error": "boom"}, code=500)
            return

        if self.scope.require_auth and not self.verified:
            self.reply(
                request.id, method, {"error": "not verified"}, code=NOT_VERIFIED_CODE
            )
            return

        self.reply(request.id, method, self.scope.canned_result(method, params))


class MockScope:
    """A stand-in Scope serving the wire protocol on :4700/:4800/:4804."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        command_port: int = 4700,
        tele_port: int = image.TELE_PORT,
        wide_port: int = image.WIDE_PORT,
        *,
        public_pem: bytes | None = None,
        require_auth: bool = True,
        frame_interval: float = 0.05,
        frame_count: int | None = None,
        frame_size: int = 1024,
        tele_resolution: tuple[int, int] = (1920, 1080),
        wide_resolution: tuple[int, int] = (1280, 720),
    ) -> None:
        self.host = host
        self._command_port = command_port
        self._tele_port = tele_port
        self._wide_port = wide_port
        self.public_key = auth.load_public_key(public_pem) if public_pem else None
        self.require_auth = require_auth
        self.frame_interval = frame_interval
        self.frame_count = frame_count
        self.frame_size = frame_size
        self.tele_resolution = tele_resolution
        self.wide_resolution = wide_resolution

        self.heartbeat_silent = False
        #: method -> seconds to sleep before replying (forces out-of-order replies)
        self.reply_delay: dict[str, float] = {}
        self.heartbeat_count = 0
        self._stats_lock = threading.Lock()
        self._listeners: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._clients: set[_CommandConnection] = set()
        self._clients_lock = threading.Lock()
        self._image_socks: set[socket.socket] = set()
        self._stopping = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Bind the command and image sockets and start serving."""
        for port, handler in (
            (self._command_port, self._accept_command),
            (self._tele_port, lambda s, a: self._accept_image(s, a, self.tele_resolution)),
            (self._wide_port, lambda s, a: self._accept_image(s, a, self.wide_resolution)),
        ):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.host, port))
            listener.listen(8)
            listener.settimeout(0.5)
            self._listeners.append(listener)
            thread = threading.Thread(
                target=self._accept_loop, args=(listener, handler), daemon=True
            )
            thread.start()
            self._threads.append(thread)
        logger.info(
            "mock scope listening on %s command=%s tele=%s wide=%s",
            self.host,
            self.command_port,
            self.tele_port,
            self.wide_port,
        )

    def stop(self) -> None:
        """Stop serving and close all sockets."""
        self._stopping.set()
        for listener in self._listeners:
            try:
                listener.close()
            except OSError:
                pass
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.sock.close()
            except OSError:
                pass
        for sock in list(self._image_socks):
            try:
                sock.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._listeners.clear()
        self._threads.clear()

    def __enter__(self) -> "MockScope":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -- bound ports ---------------------------------------------------------

    @property
    def command_port(self) -> int:
        return self._listeners[0].getsockname()[1] if self._listeners else self._command_port

    @property
    def tele_port(self) -> int:
        return self._listeners[1].getsockname()[1] if len(self._listeners) > 1 else self._tele_port

    @property
    def wide_port(self) -> int:
        return self._listeners[2].getsockname()[1] if len(self._listeners) > 2 else self._wide_port

    @property
    def client_count(self) -> int:
        with self._clients_lock:
            return len(self._clients)

    # -- behaviour knobs -----------------------------------------------------

    def set_heartbeat_silent(self, silent: bool) -> None:
        """When silent, stop replying to ``test_connection`` (dropped heartbeat)."""
        self.heartbeat_silent = silent

    def push_event(self, event: Mapping[str, Any]) -> None:
        """Push an event to every connected command-channel client."""
        message = {"Event": str(event.get("Event", "Mock")), "Timestamp": f"{time.time():.9f}"}
        message.update(event)
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.send(message)
            except OSError:
                pass

    def canned_result(self, method: str, params: Mapping[str, Any]) -> Any:
        """Canned results for well-known methods; echo otherwise."""
        canned = {
            "get_device_state": {
                "mode": "idle",
                "fw_version": "9.75",
                "battery_capacity": 88,
                "is_verified": True,
            },
            "scope_get_equ_coord": {"ra": 17.093056, "dec": 34.349722},
            "get_view_state": {"state": "Idle", "mode": "star"},
        }
        if method in canned:
            return canned[method]
        return {"echo_method": method, "echo_params": dict(params)}

    # -- internals -----------------------------------------------------------

    def _accept_loop(self, listener: socket.socket, handler: Any) -> None:
        while not self._stopping.is_set():
            try:
                sock, address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(
                target=handler, args=(sock, address), daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def _accept_command(self, sock: socket.socket, address: Any) -> None:
        client = _CommandConnection(self, sock, address)
        with self._clients_lock:
            self._clients.add(client)
        decoder = protocol.MessageDecoder()
        try:
            while not self._stopping.is_set():
                data = sock.recv(65536)
                if not data:
                    break
                for message in decoder.feed(data):
                    decoded = protocol.classify(message)
                    if isinstance(decoded, protocol.Request):
                        client.handle(decoded)
        except OSError:
            pass
        finally:
            with self._clients_lock:
                self._clients.discard(client)
            try:
                sock.close()
            except OSError:
                pass

    def _accept_image(
        self, sock: socket.socket, address: Any, resolution: tuple[int, int]
    ) -> None:
        """Serve binary frames on an image socket (:4800 tele / :4804 wide)."""
        with self._clients_lock:
            self._image_socks.add(sock)
        width, height = resolution
        seq = 0
        try:
            sock.settimeout(0.1)
            while not self._stopping.is_set():
                # Drain (and ignore) any JSON commands the client sends first,
                # e.g. {"id": 21, "method": "begin_streaming"}.
                try:
                    if not sock.recv(65536):
                        break  # client closed the image socket
                except socket.timeout:
                    pass
                if self.frame_count is not None and seq >= self.frame_count:
                    break
                sock.sendall(
                    image.encode_frame(
                        make_frame_payload(seq, self.frame_size),
                        code=0,
                        id=seq,
                        width=width,
                        height=height,
                    )
                )
                seq += 1
                time.sleep(self.frame_interval)
        except OSError:
            pass
        finally:
            with self._clients_lock:
                self._image_socks.discard(sock)
            try:
                sock.close()
            except OSError:
                pass


def main(argv: list[str] | None = None) -> None:
    """Run a standalone mock Scope on the default ports."""
    parser = argparse.ArgumentParser(description="Mock Seestar scope (wire protocol test double)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--command-port", type=int, default=4700)
    parser.add_argument("--tele-port", type=int, default=image.TELE_PORT)
    parser.add_argument("--wide-port", type=int, default=image.WIDE_PORT)
    parser.add_argument(
        "--public-key",
        type=Path,
        default=None,
        help="PEM public key used to verify client handshakes",
    )
    parser.add_argument(
        "--save-private-key",
        type=Path,
        default=None,
        help="write a freshly generated test private key here (for clients)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    public_pem = args.public_key.read_bytes() if args.public_key else None
    if public_pem is None:
        private_pem, public_pem = auth.generate_keypair()
        if args.save_private_key:
            args.save_private_key.write_bytes(private_pem)
            logger.info("wrote test private key to %s", args.save_private_key)
        else:
            logger.warning("no --public-key given; generated an ephemeral keypair")

    scope = MockScope(
        args.host,
        args.command_port,
        args.tele_port,
        args.wide_port,
        public_pem=public_pem,
    )
    scope.start()
    logger.info(
        "mock scope on %s:%s (images %s/%s) — Ctrl+C to stop",
        args.host,
        scope.command_port,
        scope.tele_port,
        scope.wide_port,
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        scope.stop()


if __name__ == "__main__":
    main()
