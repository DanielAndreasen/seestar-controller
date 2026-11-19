"""Preview stream: decoded live frames from the :4800 (tele) / :4804 (wide) sockets."""

import socket
import threading
import time

import numpy as np
import pytest

from seestar_controller.link import image
from seestar_controller.link.mock_scope import (
    FRAME_MAGIC,
    MockScope,
    make_frame_payload,
    make_jpeg_payload,
)
from seestar_controller.link.preview import (
    FrameThrottle,
    PreviewFrame,
    PreviewStream,
    decode_payload,
    is_jpeg,
)


def _jpeg_scope(keypair, **kwargs):
    """A running mock Scope streaming JPEG gradient Preview frames."""
    _private_pem, public_pem = keypair
    params = dict(
        public_pem=public_pem,
        frame_format="jpeg",
        frame_count=8,
        frame_interval=0.01,
        tele_resolution=(64, 48),
        wide_resolution=(32, 24),
    )
    params.update(kwargs)
    scope = MockScope("127.0.0.1", 0, 0, 0, **params)
    scope.start()
    return scope


@pytest.fixture()
def jpeg_scope(keypair):
    scope = _jpeg_scope(keypair)
    yield scope
    scope.stop()


class Collector:
    """Thread-safe Preview callback collecting every delivered frame."""

    def __init__(self):
        self.lock = threading.Lock()
        self.frames: list[PreviewFrame] = []

    def __call__(self, pf: PreviewFrame) -> None:
        with self.lock:
            self.frames.append(pf)

    def snapshot(self) -> list[PreviewFrame]:
        with self.lock:
            return list(self.frames)

    def __len__(self) -> int:
        with self.lock:
            return len(self.frames)


def _stream(scope, **kwargs) -> PreviewStream:
    params = dict(max_fps=None, queue_size=64)
    params.update(kwargs)
    return PreviewStream(
        "127.0.0.1", tele_port=scope.tele_port, wide_port=scope.wide_port, **params
    )


# -- payload decoding -----------------------------------------------------------


def test_is_jpeg_detects_soi_marker():
    assert is_jpeg(b"\xff\xd8\xff\xe0rest")
    assert not is_jpeg(make_frame_payload(0, 32))
    assert not is_jpeg(b"")


def test_decode_payload_jpeg_via_pil():
    payload = make_jpeg_payload(7, 64, 48)
    frame = image.Frame(
        code=0, id=7, width=64, height=48, size=len(payload), payload=payload
    )
    arr = decode_payload(frame)
    assert arr.dtype == np.uint8
    assert arr.shape == (48, 64, 3)
    # the frame counter survives lossy JPEG in the flat blue channel
    assert abs(float(arr[..., 2].mean()) - 7) <= 2
    # the gradient channels vary across the image
    assert arr[0, 0, 0] < arr[0, -1, 0]
    assert arr[0, 0, 1] < arr[-1, 0, 1]


def test_decode_payload_raw_grayscale_uses_header_resolution():
    payload = bytes(range(12))
    frame = image.Frame(code=0, id=0, width=4, height=3, size=12, payload=payload)
    arr = decode_payload(frame)
    assert arr.shape == (3, 4)
    assert arr.dtype == np.uint8
    assert arr[2, 3] == 11


def test_decode_payload_raw_rgb_uses_header_resolution():
    payload = bytes(range(12))
    frame = image.Frame(code=0, id=0, width=2, height=2, size=12, payload=payload)
    arr = decode_payload(frame)
    assert arr.shape == (2, 2, 3)
    assert arr.dtype == np.uint8


def test_decode_payload_raw_fallback_is_flat_array():
    payload = make_frame_payload(3, 100)  # size does not match width*height
    frame = image.Frame(
        code=0, id=3, width=1920, height=1080, size=100, payload=payload
    )
    arr = decode_payload(frame)
    assert arr.shape == (100,)
    assert arr.dtype == np.uint8
    assert arr[: len(FRAME_MAGIC)].tobytes() == FRAME_MAGIC


# -- max-FPS throttle -------------------------------------------------------------


def test_frame_throttle_limits_rate_with_fake_clock():
    times = iter([0.0, 0.05, 0.10, 0.15, 0.20, 0.25])
    throttle = FrameThrottle(10.0, clock=lambda: next(times))
    assert [throttle.allow() for _ in range(6)] == [True, False, True, False, True, False]


def test_frame_throttle_none_allows_everything():
    throttle = FrameThrottle(None, clock=lambda: 0.0)
    assert all(throttle.allow() for _ in range(100))


def test_frame_throttle_rejects_non_positive_fps():
    with pytest.raises(ValueError):
        FrameThrottle(0)
    with pytest.raises(ValueError):
        FrameThrottle(-5)


# -- streaming against the mock ---------------------------------------------------


def test_default_image_ports_are_4800_and_4804():
    stream = PreviewStream("127.0.0.1")
    assert stream.tele_port == image.TELE_PORT == 4800
    assert stream.wide_port == image.WIDE_PORT == 4804


def test_stream_receives_all_n_frames_decoded(jpeg_scope):
    """Mock streams N frames; the callback receives all N decoded correctly
    (shape/dtype, counter increments) — without any control connection."""
    collector = Collector()
    stream = _stream(jpeg_scope)
    stream.add_callback(collector)
    stream.start(tele=True)
    try:
        assert stream.running
        assert stream.wait_for_frames(8, timeout=5.0)
    finally:
        stream.stop()
    assert not stream.running
    frames = collector.snapshot()
    assert len(frames) == 8
    for i, pf in enumerate(frames):
        assert pf.source == "tele"
        assert pf.frame.id == i  # wire counter increments
        assert pf.image.shape == (48, 64, 3)
        assert pf.image.dtype == np.uint8
        # counter encoded in the image (blue channel) increments too
        assert abs(float(pf.image[..., 2].mean()) - i) <= 2
    # independent of the control connection: no :4700 client ever connected
    assert jpeg_scope.client_count == 0


def test_stream_wide_source(jpeg_scope):
    collector = Collector()
    stream = _stream(jpeg_scope)
    stream.add_callback(collector)
    stream.start(tele=False, wide=True)
    try:
        assert stream.wait_for_frames(8, timeout=5.0)
    finally:
        stream.stop()
    frames = collector.snapshot()
    assert len(frames) == 8
    assert all(pf.source == "wide" for pf in frames)
    assert all(pf.image.shape == (24, 32, 3) for pf in frames)
    assert [pf.frame.id for pf in frames] == list(range(8))


def test_stream_both_sources_at_once(jpeg_scope):
    collector = Collector()
    stream = _stream(jpeg_scope)
    stream.add_callback(collector)
    stream.start(tele=True, wide=True)
    try:
        assert stream.wait_for_frames(16, timeout=5.0)
    finally:
        stream.stop()
    frames = collector.snapshot()
    assert len(frames) == 16
    assert {pf.source for pf in frames} == {"tele", "wide"}
    assert stream.frames_delivered == {"tele": 8, "wide": 8}


def test_partial_frames_across_tcp_segments_are_reassembled(keypair):
    """The mock sends each frame in 13-byte chunks; the stream must still
    reassemble and decode every frame."""
    scope = _jpeg_scope(keypair, frame_count=5, chunk_size=13)
    try:
        collector = Collector()
        stream = _stream(scope)
        stream.add_callback(collector)
        stream.start(tele=True)
        try:
            assert stream.wait_for_frames(5, timeout=5.0)
        finally:
            stream.stop()
        frames = collector.snapshot()
        assert [pf.frame.id for pf in frames] == [0, 1, 2, 3, 4]
        assert all(pf.image.shape == (48, 64, 3) for pf in frames)
        assert all(
            abs(float(pf.image[..., 2].mean()) - pf.frame.id) <= 2 for pf in frames
        )
    finally:
        scope.stop()


def test_start_stop_and_restart(keypair):
    scope = _jpeg_scope(keypair, frame_count=None, frame_interval=0.01)
    try:
        collector = Collector()
        stream = _stream(scope)
        stream.add_callback(collector)
        stream.stop()  # stop before start is a no-op
        stream.start(tele=True)
        assert stream.running
        assert stream.active_sources == ("tele",)
        assert stream.wait_for_frames(3, timeout=5.0)
        stream.stop()
        assert not stream.running
        assert stream.active_sources == ()
        count_after_stop = len(collector)
        time.sleep(0.3)
        assert len(collector) == count_after_stop  # no callbacks after stop
        # the stream restarts on its own socket
        stream.start(tele=True)
        assert stream.wait_for_frames(3, timeout=5.0)
        stream.stop()
        assert len(collector) >= 6
    finally:
        scope.stop()


def test_start_twice_raises_and_start_without_source_raises(jpeg_scope):
    stream = _stream(jpeg_scope)
    with pytest.raises(ValueError):
        stream.start(tele=False, wide=False)
    stream.start(tele=True)
    try:
        with pytest.raises(RuntimeError):
            stream.start(tele=True)
    finally:
        stream.stop()
    stream.stop()  # stop is idempotent


def test_start_against_closed_port_raises():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    stream = PreviewStream("127.0.0.1", tele_port=port, wide_port=port)
    with pytest.raises(OSError):
        stream.start(tele=True)
    assert not stream.running


def test_stream_ends_when_scope_closes_image_socket(jpeg_scope):
    """frame_count=8: after 8 frames the mock closes the socket (EOF) and the
    stream reports itself as no longer running."""
    collector = Collector()
    stream = _stream(jpeg_scope)
    stream.add_callback(collector)
    stream.start(tele=True)
    deadline = time.monotonic() + 5
    while stream.running and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not stream.running
    assert stream.active_sources == ()
    assert len(collector) == 8
    stream.stop()  # still safe after the natural end


def test_max_fps_throttles_delivery(keypair):
    """Mock streams ~200 fps; with max_fps=20 only ~20 fps reach the callback."""
    scope = _jpeg_scope(
        keypair, frame_count=None, frame_interval=0.005, tele_resolution=(16, 12)
    )
    try:
        collector = Collector()
        stream = _stream(scope, max_fps=20.0, queue_size=256)
        stream.add_callback(collector)
        stream.start(tele=True)
        time.sleep(0.6)
        stream.stop()
        read = stream.frames_read["tele"]
        delivered = stream.frames_delivered["tele"]
        assert read >= 30  # the mock streamed fast
        assert 1 <= delivered <= 20 * 0.8 + 1  # throttled to ~20 fps
        assert delivered < read  # frames were dropped for display
        assert len(collector) == delivered
    finally:
        scope.stop()


def test_slow_callback_does_not_block_socket_reader(keypair):
    """A slow callback must not stall the reader: all frames are still read
    from the socket (excess ones are dropped at the bounded queue)."""
    scope = _jpeg_scope(
        keypair, frame_count=10, frame_interval=0.005, tele_resolution=(16, 12)
    )
    try:

        def slow(pf: PreviewFrame) -> None:
            time.sleep(0.15)

        collector = Collector()
        stream = _stream(scope, queue_size=2)
        stream.add_callback(slow)
        stream.add_callback(collector)
        stream.start(tele=True)
        assert stream.wait_for_frames(1, timeout=5.0)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and stream.frames_read["tele"] < 10:
            time.sleep(0.02)
        stream.stop()
        assert stream.frames_read["tele"] == 10  # reader never blocked
        assert len(collector) >= 1
    finally:
        scope.stop()


def test_callback_exception_does_not_kill_stream(jpeg_scope):
    def boom(pf: PreviewFrame) -> None:
        raise RuntimeError("callback bug")

    collector = Collector()
    stream = _stream(jpeg_scope)
    stream.add_callback(boom)
    stream.add_callback(collector)
    stream.start(tele=True)
    try:
        assert stream.wait_for_frames(8, timeout=5.0)
    finally:
        stream.stop()
    assert len(collector) == 8


def test_remove_callback_and_on_frame_decorator(jpeg_scope):
    collector = Collector()
    stream = _stream(jpeg_scope)
    stream.add_callback(collector)
    stream.remove_callback(collector)

    @stream.on_frame
    def cb(pf: PreviewFrame) -> None:
        collector(pf)

    stream.start(tele=True)
    try:
        assert stream.wait_for_frames(8, timeout=5.0)
    finally:
        stream.stop()
    assert len(collector) == 8


def test_wait_for_frames_times_out(jpeg_scope):
    stream = _stream(jpeg_scope)
    assert stream.wait_for_frames(1, timeout=0.2) is False
