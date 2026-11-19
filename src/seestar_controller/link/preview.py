"""Preview stream: the Scope's live image stream on :4800 (tele) / :4804 (wide).

The **Preview** (GLOSSARY) is the live image stream of the current target.
Unlike the command channel (:4700), the image sockets carry raw binary
frames (see :mod:`seestar_controller.link.image`): a 20-byte header plus a
payload that is a JPEG on real hardware.

:class:`PreviewStream` opens its own sockets to the tele and/or wide image
ports — independent of the control connection, so the Preview can start and
stop on its own — reads frames with
:func:`~seestar_controller.link.image.read_frame` (which reassembles frames
split across TCP segments), decodes each payload into a numpy image array
(:func:`decode_payload`: JPEG via PIL, raw via the header's width/height),
throttles to a maximum display rate (:class:`FrameThrottle`), and delivers
:class:`PreviewFrame` objects to registered callbacks on a dispatcher
thread, so a slow callback never blocks the socket reader.

NOTE (user AFK, no real scope — docs/wiki/assumptions.md): exercised against
the mock Scope (:mod:`seestar_controller.link.mock_scope`), which can stream
synthetic JPEG gradient frames (``frame_format="jpeg"``).

Demo against a local mock Scope:
``uv run python -m seestar_controller.link.preview``
"""

from __future__ import annotations

import argparse
import io
import logging
import queue
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
from PIL import Image

from . import image
from .image import TELE_PORT, WIDE_PORT, Frame

logger = logging.getLogger(__name__)

#: JPEG SOI marker — payloads starting with it are decoded via PIL.
JPEG_SOI = b"\xff\xd8"

#: Source names of the two image sockets.
TELE = "tele"
WIDE = "wide"

#: A Preview callback: called with one :class:`PreviewFrame` per delivered
#: frame, on the stream's dispatcher thread.
PreviewCallback = Callable[["PreviewFrame"], None]


def is_jpeg(payload: bytes) -> bool:
    """True when the payload starts with the JPEG SOI marker."""
    return payload[:2] == JPEG_SOI


def decode_payload(frame: Frame) -> np.ndarray:
    """Decode one frame's payload into a numpy image array.

    JPEG payloads (what real hardware sends) are decoded with PIL into an
    RGB array ``(height, width, 3)`` uint8. Raw payloads use the header's
    ``width``/``height``: ``width*height`` bytes -> grayscale
    ``(height, width)``, ``width*height*3`` bytes -> RGB
    ``(height, width, 3)``; anything else is returned as a flat uint8 array
    of the payload bytes.
    """
    payload = frame.payload
    if is_jpeg(payload):
        with Image.open(io.BytesIO(payload)) as img:
            return np.asarray(img.convert("RGB"))
    if frame.width > 0 and frame.height > 0:
        if frame.size == frame.width * frame.height:
            return np.frombuffer(payload, dtype=np.uint8).reshape(
                frame.height, frame.width
            ).copy()
        if frame.size == frame.width * frame.height * 3:
            return np.frombuffer(payload, dtype=np.uint8).reshape(
                frame.height, frame.width, 3
            ).copy()
    return np.frombuffer(payload, dtype=np.uint8).copy()


@dataclass(frozen=True)
class PreviewFrame:
    """One decoded Preview frame delivered to callbacks.

    ``image`` is the decoded numpy array (see :func:`decode_payload`),
    ``frame`` the raw wire frame (its ``id`` is the scope's frame counter),
    ``source`` which image socket the frame came from ("tele"/"wide"), and
    ``timestamp`` the monotonic time the frame was read.
    """

    source: str
    image: np.ndarray
    frame: Frame
    timestamp: float

    @property
    def id(self) -> int:
        return self.frame.id

    @property
    def width(self) -> int:
        return self.frame.width

    @property
    def height(self) -> int:
        return self.frame.height


class FrameThrottle:
    """Max-FPS gate: :meth:`allow` returns True at most ``max_fps`` times per
    second (``None`` disables throttling)."""

    def __init__(
        self, max_fps: float | None, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if max_fps is not None and max_fps <= 0:
            raise ValueError(f"max_fps must be > 0 or None, got {max_fps}")
        self.max_fps = max_fps
        self._clock = clock
        self._min_interval = 1.0 / max_fps if max_fps is not None else 0.0
        self._last: float | None = None

    def allow(self) -> bool:
        """True when a frame may be delivered now (and records the delivery)."""
        if self.max_fps is None:
            return True
        now = self._clock()
        if self._last is not None and now - self._last < self._min_interval:
            return False
        self._last = now
        return True


class PreviewStream:
    """Live Preview stream from the Scope's image sockets.

    Independent of the control connection (:4700): the stream opens its own
    sockets to the tele (:4800) and/or wide (:4804) image ports, so it can
    start and stop on its own::

        stream = PreviewStream("192.168.1.42")
        stream.add_callback(lambda pf: show(pf.image))
        stream.start(tele=True)                 # or wide=True, or both
        ...
        stream.stop()

    Frames are read with :func:`~seestar_controller.link.image.read_frame`
    (partial frames split across TCP segments are reassembled), decoded to
    numpy arrays, throttled to ``max_fps`` per source, and delivered to the
    registered callbacks on a dispatcher thread — a slow callback never
    blocks the socket reader (excess frames are dropped instead).
    """

    def __init__(
        self,
        host: str,
        tele_port: int = TELE_PORT,
        wide_port: int = WIDE_PORT,
        *,
        max_fps: float | None = 10.0,
        connect_timeout: float = 5.0,
        queue_size: int = 16,
    ) -> None:
        if max_fps is not None and max_fps <= 0:
            raise ValueError(f"max_fps must be > 0 or None, got {max_fps}")
        self.host = host
        self.tele_port = tele_port
        self.wide_port = wide_port
        #: max delivered frames per second, per image socket (None = no limit)
        self.max_fps = max_fps
        self.connect_timeout = connect_timeout
        self._queue: queue.Queue[PreviewFrame] = queue.Queue(maxsize=queue_size)
        self._lock = threading.Lock()
        self._callbacks: list[PreviewCallback] = []
        #: stop event of the *current* generation (created fresh in start() so
        #: a straggler thread of an older generation can never be revived by a
        #: later start()); None while stopped
        self._stop: threading.Event | None = None
        self._running = False
        self._threads: dict[str, threading.Thread] = {}
        self._socks: dict[str, socket.socket] = {}
        self._dispatcher: threading.Thread | None = None
        self._read: dict[str, int] = {}
        self._delivered: dict[str, int] = {}

    # -- lifecycle -----------------------------------------------------------

    def start(self, tele: bool = True, wide: bool = False) -> None:
        """Start streaming from the selected image sockets.

        ``start(tele=True)`` streams the tele camera (:4800),
        ``start(wide=True)`` the wide camera (:4804), and
        ``start(tele=True, wide=True)`` both. The image sockets are connected
        synchronously, so an unreachable scope raises from ``start()``.
        Raises :class:`RuntimeError` when already running and
        :class:`ValueError` when neither source is selected.
        """
        with self._lock:
            if self._running:
                raise RuntimeError(
                    "preview stream is already running; call stop() first"
                )
            if not tele and not wide:
                raise ValueError(
                    "start() needs at least one source: tele=True and/or wide=True"
                )
            sources = []
            if tele:
                sources.append((TELE, self.tele_port))
            if wide:
                sources.append((WIDE, self.wide_port))
            socks: dict[str, socket.socket] = {}
            try:
                for source, port in sources:
                    sock = socket.create_connection(
                        (self.host, port), timeout=self.connect_timeout
                    )
                    sock.settimeout(None)
                    socks[source] = sock
            except BaseException:
                for sock in socks.values():
                    try:
                        sock.close()
                    except OSError:
                        pass
                raise
            # Per-generation stop event: every thread of this start() holds a
            # reference to it, so stop() can never be "undone" for them by a
            # later start() (which creates a fresh event instead of clearing
            # a shared one).
            stop_event = threading.Event()
            self._stop = stop_event
            self._running = True
            self._read = {source: 0 for source, _ in sources}
            self._delivered = {source: 0 for source, _ in sources}
            self._socks = socks
            self._dispatcher = threading.Thread(
                target=self._dispatch_loop,
                args=(stop_event,),
                name="preview-dispatch",
                daemon=True,
            )
            self._dispatcher.start()
            for source, _port in sources:
                thread = threading.Thread(
                    target=self._reader_loop,
                    args=(source, socks[source], stop_event),
                    name=f"preview-{source}",
                    daemon=True,
                )
                self._threads[source] = thread
                thread.start()
        logger.info(
            "preview stream started on %s (tele=%s wide=%s, max_fps=%s)",
            self.host,
            tele,
            wide,
            self.max_fps,
        )

    def stop(self) -> None:
        """Stop the Preview stream: close the image sockets and stop the
        reader/dispatcher threads. Idempotent; safe to call before start().

        The dispatcher exits as soon as its stop event is set (dropping any
        frames still queued) and is joined without a timeout, so when
        ``stop()`` returns no callback of this generation is running or will
        run anymore.
        """
        with self._lock:
            self._running = False
            stop_event = self._stop
            self._stop = None
            if stop_event is not None:
                stop_event.set()
            socks = list(self._socks.values())
            self._socks.clear()
            threads = list(self._threads.values())
            self._threads.clear()
            dispatcher = self._dispatcher
            self._dispatcher = None
        for sock in socks:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        current = threading.current_thread()
        for thread in threads:
            if thread is not current and thread.is_alive():
                thread.join(timeout=2.0)
        if dispatcher is not None and dispatcher is not current and dispatcher.is_alive():
            # No timeout: the dispatcher checks its (now set) stop event
            # before every queue get and exits right after the in-flight
            # callback, so this join cannot outlive one callback.
            dispatcher.join()
        logger.info("preview stream stopped")

    def __enter__(self) -> "PreviewStream":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    @property
    def running(self) -> bool:
        """True while the stream is started and at least one image socket is
        still being read (False after stop() and after every source hit EOF)."""
        with self._lock:
            return self._running

    @property
    def active_sources(self) -> tuple[str, ...]:
        """Sources ("tele"/"wide") with a live reader thread right now."""
        with self._lock:
            return tuple(sorted(self._threads))

    @property
    def frames_read(self) -> dict[str, int]:
        """Frames read from each image socket (before throttling)."""
        with self._lock:
            return dict(self._read)

    @property
    def frames_delivered(self) -> dict[str, int]:
        """Frames delivered to callbacks per source (after throttling)."""
        with self._lock:
            return dict(self._delivered)

    def wait_for_frames(self, count: int, timeout: float | None = None) -> bool:
        """Block until at least ``count`` frames were delivered in total.

        Returns True when the count was reached, False on timeout or when the
        stream ended before reaching it.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                total = sum(self._delivered.values())
                running = self._running
                threads = bool(self._threads)
            if total >= count:
                return True
            if not running and not threads and self._queue.empty():
                return False
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    # -- callbacks -------------------------------------------------------------

    def add_callback(self, callback: PreviewCallback) -> None:
        """Register ``callback`` to receive every delivered :class:`PreviewFrame`."""
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    def remove_callback(self, callback: PreviewCallback) -> None:
        """Remove a callback registered with :meth:`add_callback`."""
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    def on_frame(self, callback: PreviewCallback | None = None) -> Any:
        """Register a Preview callback; decorator form ``@stream.on_frame``."""
        if callback is None:

            def decorator(fn: PreviewCallback) -> PreviewCallback:
                self.add_callback(fn)
                return fn

            return decorator
        self.add_callback(callback)
        return callback

    # -- internals ---------------------------------------------------------------

    def _reader_loop(
        self, source: str, sock: socket.socket, stop_event: threading.Event
    ) -> None:
        """Read frames from one image socket, throttle, decode, enqueue.

        ``stop_event`` is this generation's stop event (created in
        :meth:`start`), never a shared one, so a late-exiting reader of an
        old generation cannot disturb a newer one.
        """
        throttle = FrameThrottle(self.max_fps)
        try:
            while not stop_event.is_set():
                try:
                    frame = image.read_frame(sock)
                except (ValueError, OSError) as exc:
                    if not stop_event.is_set():
                        logger.warning(
                            "preview %s: frame read failed: %s", source, exc
                        )
                    break
                if frame is None:
                    break  # EOF: the scope closed the image socket
                with self._lock:
                    self._read[source] = self._read.get(source, 0) + 1
                if not throttle.allow():
                    continue  # over max_fps: drop the frame for display
                try:
                    arr = decode_payload(frame)
                except Exception:
                    logger.exception(
                        "preview %s: could not decode frame id=%s", source, frame.id
                    )
                    continue
                preview_frame = PreviewFrame(
                    source=source,
                    image=arr,
                    frame=frame,
                    timestamp=time.monotonic(),
                )
                try:
                    self._queue.put_nowait(preview_frame)
                except queue.Full:
                    pass  # dispatcher busy: drop, keep reading the socket
        finally:
            # Generation-safe cleanup: only remove the entries that still
            # belong to *this* thread/socket — a newer generation may already
            # have replaced them in the shared dicts.
            current = threading.current_thread()
            with self._lock:
                if self._threads.get(source) is current:
                    self._threads.pop(source, None)
                if self._socks.get(source) is sock:
                    self._socks.pop(source, None)
                if (
                    self._stop is stop_event
                    and not self._threads
                    and not stop_event.is_set()
                ):
                    # every source ended (EOF): the stream is no longer running
                    self._running = False
            try:
                sock.close()
            except OSError:
                pass

    def _dispatch_loop(self, stop_event: threading.Event) -> None:
        """Deliver queued frames to the callbacks (never blocks the readers).

        Exits as soon as ``stop_event`` is set, dropping frames still queued:
        a slow callback must not keep the dispatcher (and thus stop())
        alive, and a straggler of an old generation must never consume
        frames of a newer one.
        """
        while not stop_event.is_set():
            try:
                preview_frame = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            with self._lock:
                callbacks = list(self._callbacks)
            for callback in callbacks:
                try:
                    callback(preview_frame)
                except Exception:
                    logger.exception("preview callback failed")
            with self._lock:
                self._delivered[preview_frame.source] = (
                    self._delivered.get(preview_frame.source, 0) + 1
                )


def main(argv: list[str] | None = None) -> None:
    """Demo against a local mock Scope: stream Preview frames and print them."""
    parser = argparse.ArgumentParser(
        description="Preview stream demo against a local mock Scope"
    )
    parser.add_argument("--frames", type=int, default=5, help="frames to stream")
    parser.add_argument("--fps", type=float, default=20.0, help="mock streaming rate")
    parser.add_argument("--max-fps", type=float, default=10.0, help="display throttle")
    parser.add_argument("--wide", action="store_true", help="stream the wide socket")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from . import auth
    from .mock_scope import MockScope

    _private_pem, public_pem = auth.generate_keypair()
    scope = MockScope(
        "127.0.0.1",
        0,
        0,
        0,
        public_pem=public_pem,
        frame_format="jpeg",
        frame_count=args.frames,
        frame_interval=1.0 / args.fps,
        tele_resolution=(320, 240),
        wide_resolution=(320, 240),
    )
    scope.start()
    try:
        stream = PreviewStream(
            "127.0.0.1",
            tele_port=scope.tele_port,
            wide_port=scope.wide_port,
            max_fps=args.max_fps,
        )

        @stream.on_frame
        def show(pf: PreviewFrame) -> None:
            print(
                f"  preview {pf.source} frame id={pf.id} "
                f"image={pf.image.shape} {pf.image.dtype} "
                f"counter(blue)={pf.image[..., 2].mean():.1f}"
            )

        stream.start(tele=not args.wide, wide=args.wide)
        stream.wait_for_frames(args.frames, timeout=10.0)
        stream.stop()
    finally:
        scope.stop()


if __name__ == "__main__":
    main()
