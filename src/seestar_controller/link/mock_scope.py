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

Beyond auth and heartbeat, the mock carries a device state (mode, current
target, stacking progress) and serves the device-level commands the facade
uses: ``get_scope_state``, ``scope_goto_target`` (simulated slew of
:attr:`MockScope.slew_duration` seconds, then a pushed ``ScopeGoto``
"complete" event and mode back to idle), ``scope_park`` (mode ->
parked, ``ScopePark`` event pushed), and stacking control:
``iscope_start_stacking`` (validates exposure/gain/mosaic ranges, mode ->
stacking, then pushes ``Stack`` progress events with frame count + elapsed
until stopped) and ``iscope_stop_stacking`` (mode -> idle, final ``Stack``
event). Invalid stacking params are rejected with a non-zero reply code.

The image sockets stream the **Preview** (GLOSSARY): each accepted
connection gets a loop pushing synthetic frames at :attr:`frame_interval`
(:attr:`stream_fps`), capped by ``frame_count``. ``frame_format="raw"``
sends ``MOCKFRAME`` + counter payloads; ``frame_format="jpeg"`` sends a
JPEG-encoded RGB gradient with the frame counter in the blue channel (what
real hardware sends). ``chunk_size`` splits each frame into small TCP
segments (partial-frame reassembly tests), and :meth:`stop_streaming` /
:meth:`start_streaming` gate the push.

NOTE (user AFK, no real scope / no PEM key — docs/wiki/assumptions.md):
this mock is the only counterpart the client is tested against. Only the
20-byte frame framing and the synthetic payloads are exercised; real JPEGs
from hardware are unverified.

Run standalone: ``uv run python -m seestar_controller.link.mock_scope``
"""

from __future__ import annotations

import argparse
import copy
import io
import json
import logging
import secrets
import select
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from PIL import Image

from . import auth, image, protocol, stacking

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


def make_jpeg_payload(seq: int, width: int, height: int, quality: int = 95) -> bytes:
    """Synthetic JPEG Preview frame: an RGB gradient with the frame counter
    ``seq`` in the flat blue channel.

    The blue channel is a uniform field of ``seq % 256``, which survives
    lossy JPEG encoding within a couple of gray levels, so a decoder can
    recover the counter from the decoded image (see
    :func:`seestar_controller.link.preview.decode_payload`).
    """
    ramp_x = np.linspace(0, 255, width, dtype=np.uint8)
    ramp_y = np.linspace(0, 255, height, dtype=np.uint8)
    arr = np.empty((height, width, 3), dtype=np.uint8)
    arr[..., 0] = ramp_x[np.newaxis, :]
    arr[..., 1] = ramp_y[:, np.newaxis]
    arr[..., 2] = seq % 256
    buf = io.BytesIO()
    Image.fromarray(arr, "RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


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

        handled, result, code = self.scope.handle_scope_command(method, params)
        if handled:
            self.reply(request.id, method, result, code=code)
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
        frame_format: str = "raw",
        chunk_size: int | None = None,
        tele_resolution: tuple[int, int] = (1920, 1080),
        wide_resolution: tuple[int, int] = (1280, 720),
        slew_duration: float = 0.5,
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
        if frame_format not in ("raw", "jpeg"):
            raise ValueError(
                f"frame_format must be 'raw' or 'jpeg', got {frame_format!r}"
            )
        #: payload flavour of the streamed Preview frames: "raw" (MOCKFRAME
        #: + counter) or "jpeg" (JPEG gradient with counter, like real hw)
        self.frame_format = frame_format
        if chunk_size is not None and chunk_size <= 0:
            raise ValueError(f"chunk_size must be > 0 or None, got {chunk_size}")
        #: when set, each frame is sent in TCP segments of this many bytes
        #: (partial-frame reassembly test knob)
        self.chunk_size = chunk_size
        self.tele_resolution = tele_resolution
        self.wide_resolution = wide_resolution
        #: gate on the image-socket streaming loops (start/stop_streaming)
        self._stream_gate = threading.Event()
        self._stream_gate.set()
        #: simulated slew time in seconds for scope_goto_target (test knob)
        self.slew_duration = slew_duration
        #: real seconds between pushed Stack progress events while stacking
        self.stack_event_interval = 0.2
        #: simulated elapsed time per stacked sub-frame (GLOSSARY: 10 s sub-frame)
        self.subframe_ms = 10000

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

        # -- device state (what get_scope_state reports) ----------------------
        self._scope_state_lock = threading.Lock()
        self._scope_state: dict[str, Any] = {
            "mode": "idle",
            "target": None,
            "stacking": {"state": "idle", "lapse_ms": 0, "frames": 0},
        }
        #: last accepted goto target {"ra": ..., "dec": ...} (test hook)
        self.last_goto: dict[str, float] | None = None
        #: last accepted stacking params (wire shape, test hook)
        self.last_stack_params: dict[str, Any] | None = None
        self._stack_stop = threading.Event()
        self._stack_thread: threading.Thread | None = None

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
        self._stack_stop.set()  # wake the stacking loop so it exits promptly
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

    # -- preview streaming knobs -------------------------------------------------

    @property
    def streaming(self) -> bool:
        """True while the image sockets push Preview frames."""
        return self._stream_gate.is_set()

    def start_streaming(self) -> None:
        """Resume pushing Preview frames on the image sockets."""
        self._stream_gate.set()

    def stop_streaming(self) -> None:
        """Pause pushing Preview frames; connected image clients stay
        connected and simply receive nothing until :meth:`start_streaming`."""
        self._stream_gate.clear()

    @property
    def stream_fps(self) -> float:
        """The image sockets' streaming rate in frames per second."""
        return 1.0 / self.frame_interval if self.frame_interval > 0 else float("inf")

    @stream_fps.setter
    def stream_fps(self, fps: float) -> None:
        if fps <= 0:
            raise ValueError(f"stream_fps must be > 0, got {fps}")
        self.frame_interval = 1.0 / fps

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

    def send_raw(self, data: bytes) -> None:
        """Write raw bytes to every connected command-channel client (test hook,
        e.g. to inject a malformed JSON line)."""
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            try:
                with client.write_lock:
                    client.sock.sendall(data)
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

    # -- device state: get_scope_state / goto / park / stacking ---------------

    def scope_state(self) -> dict[str, Any]:
        """A deep copy of the mock Scope's current state (mode/target/stacking)."""
        with self._scope_state_lock:
            return copy.deepcopy(self._scope_state)

    def set_stacking_progress(
        self, state: str, *, lapse_ms: int = 0, frames: int = 0
    ) -> None:
        """Test knob: set the stacking progress reported by ``get_scope_state``."""
        with self._scope_state_lock:
            self._scope_state["stacking"] = {
                "state": state,
                "lapse_ms": lapse_ms,
                "frames": frames,
            }

    def begin_goto(self, ra: float, dec: float) -> None:
        """Start a simulated slew to (ra, dec).

        Mode goes to ``slewing`` immediately and a ``ScopeGoto`` "slewing"
        event is pushed; after :attr:`slew_duration` seconds the mode goes
        back to ``idle`` and a ``ScopeGoto`` "complete" event is pushed.
        """
        with self._scope_state_lock:
            self._scope_state["mode"] = "slewing"
            self._scope_state["target"] = {"ra": ra, "dec": dec}
            self.last_goto = {"ra": ra, "dec": dec}
        self.push_event({"Event": "ScopeGoto", "state": "slewing", "ra": ra, "dec": dec})
        thread = threading.Thread(
            target=self._slew, args=(ra, dec), name="mock-slew", daemon=True
        )
        self._threads.append(thread)
        thread.start()

    def _slew(self, ra: float, dec: float) -> None:
        time.sleep(self.slew_duration)
        if self._stopping.is_set():
            return
        with self._scope_state_lock:
            if self._scope_state["mode"] == "slewing":
                self._scope_state["mode"] = "idle"
        self.push_event(
            {"Event": "ScopeGoto", "state": "complete", "ra": ra, "dec": dec}
        )

    def park(self) -> None:
        """Park the mock Scope: mode -> ``parked``, ``ScopePark`` event pushed."""
        with self._scope_state_lock:
            self._scope_state["mode"] = "parked"
        self.push_event({"Event": "ScopePark", "state": "parked"})

    def start_stacking(
        self, exposure: float, gain: int, mosaic: Any = False
    ) -> dict[str, Any]:
        """Begin stacking a Stack: validate params, mode -> ``stacking``, and
        start pushing ``Stack`` progress events (frame count + elapsed).

        ``exposure`` is the sub-frame exposure in seconds; ``mosaic`` is a
        bool or a ``(pan, tilt)`` framing in degrees (see
        :func:`seestar_controller.link.stacking.stack_request_params`).
        Raises ``ValueError`` on out-of-range params or when already
        stacking. Returns the accepted wire params.
        """
        params = stacking.stack_request_params(exposure, gain, mosaic)
        with self._scope_state_lock:
            if self._scope_state["mode"] == "stacking":
                raise ValueError("already stacking")
            self._scope_state["mode"] = "stacking"
            self._scope_state["stacking"] = {
                "state": "working",
                "lapse_ms": 0,
                "frames": 0,
            }
            self.last_stack_params = dict(params)
        self._stack_stop.clear()
        thread = threading.Thread(
            target=self._stack_loop, name="mock-stack", daemon=True
        )
        self._stack_thread = thread
        self._threads.append(thread)
        thread.start()
        # initial progress event: stacking has begun, no sub-frame yet
        self.push_event(
            {"Event": stacking.STACK_EVENT, "state": "working", "lapse_ms": 0, "frames": 0}
        )
        return dict(params)

    def _stack_loop(self) -> None:
        """Push a ``Stack`` progress event per simulated stacked sub-frame."""
        while True:
            self._stack_stop.wait(self.stack_event_interval)
            with self._scope_state_lock:
                if self._stack_stop.is_set() or self._stopping.is_set():
                    return
                progress = self._scope_state["stacking"]
                progress["frames"] += 1
                progress["lapse_ms"] += self.subframe_ms
                frames = progress["frames"]
                lapse_ms = progress["lapse_ms"]
            self.push_event(
                {
                    "Event": stacking.STACK_EVENT,
                    "state": "working",
                    "lapse_ms": lapse_ms,
                    "frames": frames,
                }
            )

    def stop_stacking(self) -> dict[str, Any]:
        """Stop the current Stack: mode -> ``idle``, final ``Stack`` event.

        Raises ``ValueError`` when not stacking. Returns the final progress
        (mode, state, frames, lapse_ms).
        """
        self._stack_stop.set()
        thread = self._stack_thread
        if (
            thread is not None
            and thread is not threading.current_thread()
            and thread.is_alive()
        ):
            thread.join(timeout=2.0)
        with self._scope_state_lock:
            if self._scope_state["mode"] != "stacking":
                raise ValueError("not stacking")
            self._scope_state["mode"] = "idle"
            self._scope_state["stacking"]["state"] = "idle"
            final = dict(self._scope_state["stacking"])
        self.push_event(
            {
                "Event": stacking.STACK_EVENT,
                "state": "idle",
                "lapse_ms": final["lapse_ms"],
                "frames": final["frames"],
            }
        )
        return {
            "mode": "idle",
            "state": final["state"],
            "lapse_ms": final["lapse_ms"],
            "frames": final["frames"],
        }

    def handle_scope_command(
        self, method: str, params: Mapping[str, Any]
    ) -> tuple[bool, Any, int]:
        """Device-level commands used by the facade.

        Returns ``(handled, result, code)``; ``handled`` is False for methods
        this dispatcher does not know (fall through to canned results).
        """
        if method == "get_scope_state":
            return True, self.scope_state(), 0
        if method == "scope_goto_target":
            try:
                ra = float(params.get("ra"))
                dec = float(params.get("dec"))
            except (TypeError, ValueError):
                return True, {"error": f"ra/dec must be numbers, got {dict(params)!r}"}, -1
            if not (0.0 <= ra < 24.0 and -90.0 <= dec <= 90.0):
                return True, {"error": f"ra/dec out of range: ra={ra} dec={dec}"}, -1
            self.begin_goto(ra, dec)
            return True, {"ra": ra, "dec": dec}, 0
        if method == "scope_park":
            self.park()
            return True, {"mode": self.scope_state()["mode"]}, 0
        if method == stacking.START_STACK_METHOD:
            try:
                exposure, gain, mosaic = stacking.wire_params_to_stack_args(params)
                result = self.start_stacking(exposure, gain, mosaic)
            except (TypeError, ValueError) as exc:
                return True, {"error": f"invalid stacking params: {exc}"}, -1
            return True, result, 0
        if method == stacking.STOP_STACK_METHOD:
            try:
                return True, self.stop_stacking(), 0
            except ValueError as exc:
                return True, {"error": str(exc)}, -1
        return False, None, 0

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
                # A malformed JSON line must not kill this handler thread:
                # feed() consumes the bad line before raising, so re-scan the
                # remaining buffer and keep serving the good lines.
                while True:
                    try:
                        messages = decoder.feed(data)
                    except json.JSONDecodeError:
                        logger.warning(
                            "skipping malformed JSON line from client %s", address
                        )
                        data = b""  # re-scan whatever is still buffered
                        continue
                    break
                for message in messages:
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
        """Serve binary Preview frames on an image socket (:4800 tele / :4804 wide).

        A streaming loop pushes one synthetic frame every
        :attr:`frame_interval` (see :attr:`stream_fps`) while streaming is
        enabled (:meth:`start_streaming` / :meth:`stop_streaming`), until
        ``frame_count`` frames were sent (then the socket is closed) or the
        client disconnects. ``chunk_size`` splits each frame into small TCP
        segments (partial-frame reassembly test knob).
        """
        with self._clients_lock:
            self._image_socks.add(sock)
        width, height = resolution
        seq = 0
        try:
            while not self._stopping.is_set():
                # Drain (and ignore) any JSON commands the client sends first,
                # e.g. {"id": 21, "method": "begin_streaming"}, and detect a
                # closed client. Non-blocking: the drain must not pace (and
                # slow down) the streaming rate.
                readable, _, _ = select.select([sock], [], [], 0)
                if readable:
                    try:
                        if not sock.recv(65536):
                            break  # client closed the image socket
                    except OSError:
                        break
                if not self._stream_gate.wait(0.05):
                    continue  # streaming paused via stop_streaming()
                if self.frame_count is not None and seq >= self.frame_count:
                    break
                if self.frame_format == "jpeg":
                    payload = make_jpeg_payload(seq, width, height)
                else:
                    payload = make_frame_payload(seq, self.frame_size)
                blob = image.encode_frame(
                    payload, code=0, id=seq, width=width, height=height
                )
                self._send_blob(sock, blob)
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

    def _send_blob(self, sock: socket.socket, blob: bytes) -> None:
        """Send one frame blob, optionally split into ``chunk_size`` segments."""
        if self.chunk_size:
            for i in range(0, len(blob), self.chunk_size):
                sock.sendall(blob[i : i + self.chunk_size])
        else:
            sock.sendall(blob)


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
