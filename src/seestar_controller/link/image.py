"""Binary image frames on the Link's image sockets (:4800 tele, :4804 wide).

Unlike the command channel, the image sockets carry raw binary frames.
Each frame is a 20-byte big-endian header followed by ``size`` payload
bytes (a JPEG on real hardware), per seestar_alp's binary protocol::

    fmt = ">HHHIHHBBHH"   # 20 bytes
    _s1, _s2, _s3, size, _s5, _s6, code, id, width, height = unpack(fmt, header)

NOTE (user AFK, no real scope — see docs/wiki/assumptions.md): the mock
Scope sends synthetic payloads (see :mod:`...mock_scope`): ``MOCKFRAME`` +
counter with ``frame_format="raw"``, or a JPEG gradient with the frame
counter with ``frame_format="jpeg"``; only the framing is exercised against
the mock.
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

#: Default image socket ports: tele camera and wide camera.
TELE_PORT = 4800
WIDE_PORT = 4804

#: Big-endian frame header: >HHHIHHBBHH (20 bytes).
HEADER_FMT = ">HHHIHHBBHH"
HEADER_SIZE = struct.calcsize(HEADER_FMT)

#: Sanity cap on the frame ``size`` header field (a real JPEG frame is far
#: below this); a larger value means a corrupt/hostile header, not a frame.
MAX_FRAME_SIZE = 64 * 1024 * 1024


@dataclass(frozen=True)
class Frame:
    """One decoded image frame."""

    code: int
    id: int
    width: int
    height: int
    size: int
    payload: bytes


def encode_frame(
    payload: bytes,
    *,
    code: int = 0,
    id: int = 0,
    width: int = 0,
    height: int = 0,
) -> bytes:
    """Encode one image frame (header + payload)."""
    header = struct.pack(
        HEADER_FMT, 0, 0, 0, len(payload), 0, 0, code, id, width, height
    )
    return header + payload


def decode_header(header: bytes) -> tuple[int, int, int, int, int]:
    """Decode a 20-byte frame header -> (size, code, id, width, height)."""
    if len(header) != HEADER_SIZE:
        raise ValueError(f"frame header must be {HEADER_SIZE} bytes, got {len(header)}")
    _s1, _s2, _s3, size, _s5, _s6, code, id, width, height = struct.unpack(
        HEADER_FMT, header
    )
    return size, code, id, width, height


def _recv_exact(sock: socket.socket, num: int) -> bytes | None:
    """Read exactly ``num`` bytes; return None on EOF/error."""
    chunks = bytearray()
    while len(chunks) < num:
        chunk = sock.recv(num - len(chunks))
        if not chunk:
            return None
        chunks.extend(chunk)
    return bytes(chunks)


def read_frame(sock: socket.socket) -> Frame | None:
    """Blocking-read one frame from an image socket; None on EOF."""
    header = _recv_exact(sock, HEADER_SIZE)
    if header is None:
        return None
    size, code, id, width, height = decode_header(header)
    if size > MAX_FRAME_SIZE:
        raise ValueError(
            f"frame size {size} exceeds sanity cap {MAX_FRAME_SIZE}; corrupt header"
        )
    payload = _recv_exact(sock, size) if size > 0 else b""
    if payload is None:
        return None
    return Frame(code=code, id=id, width=width, height=height, size=size, payload=payload)
