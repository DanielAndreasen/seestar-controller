"""Binary image frame codec for the :4800 (tele) / :4804 (wide) sockets."""

import socket
import struct

import pytest

from seestar_controller.link import image
from seestar_controller.link.image import (
    HEADER_FMT,
    HEADER_SIZE,
    Frame,
    decode_header,
    encode_frame,
    read_frame,
)


def test_header_format_is_20_bytes_big_endian():
    assert HEADER_SIZE == 20
    assert struct.calcsize(HEADER_FMT) == 20


def test_encode_decode_header_roundtrip():
    payload = b"\xff\xd8jpeg-ish\xff\xd9"
    blob = encode_frame(payload, code=0, id=42, width=1920, height=1080)
    assert len(blob) == HEADER_SIZE + len(payload)
    size, code, id_, width, height = decode_header(blob[:HEADER_SIZE])
    assert (size, code, id_, width, height) == (len(payload), 0, 42, 1920, 1080)
    assert blob[HEADER_SIZE:] == payload


def test_decode_header_rejects_wrong_length():
    with pytest.raises(ValueError):
        decode_header(b"\x00" * 19)


def test_read_frame_over_socketpair():
    a, b = socket.socketpair()
    try:
        payload = bytes(range(256)) * 4
        a.sendall(encode_frame(payload, code=0, id=7, width=1280, height=720))
        frame = read_frame(b)
        assert isinstance(frame, Frame)
        assert frame.id == 7
        assert frame.width == 1280
        assert frame.height == 720
        assert frame.size == len(payload)
        assert frame.payload == payload
    finally:
        a.close()
        b.close()


def test_read_frame_handles_split_delivery():
    a, b = socket.socketpair()
    try:
        blob = encode_frame(b"x" * 5000, code=0, id=1, width=8, height=8)
        for i in range(0, len(blob), 100):
            a.sendall(blob[i : i + 100])
        frame = read_frame(b)
        assert frame is not None
        assert frame.payload == b"x" * 5000
    finally:
        a.close()
        b.close()


def test_read_frame_returns_none_on_eof():
    a, b = socket.socketpair()
    a.close()
    try:
        assert read_frame(b) is None
    finally:
        b.close()


def test_read_frame_multiple_frames_in_stream():
    a, b = socket.socketpair()
    try:
        for i in range(3):
            a.sendall(encode_frame(f"frame-{i}".encode(), id=i, width=4, height=4))
        frames = [read_frame(b) for _ in range(3)]
        assert [f.id for f in frames] == [0, 1, 2]  # ty: ignore[missing-union-attribute]
        assert [f.payload for f in frames] == [b"frame-0", b"frame-1", b"frame-2"]  # ty: ignore[missing-union-attribute]
    finally:
        a.close()
        b.close()


def test_read_frame_rejects_oversized_size_header():
    """Sanity cap: a frame header claiming an absurd payload size is rejected."""
    a, b = socket.socketpair()
    try:
        header = struct.pack(
            HEADER_FMT, 0, 0, 0, image.MAX_FRAME_SIZE + 1, 0, 0, 0, 0, 0, 0
        )
        a.sendall(header)
        with pytest.raises(ValueError, match="sanity cap"):
            read_frame(b)
    finally:
        a.close()
        b.close()
