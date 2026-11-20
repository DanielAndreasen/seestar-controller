"""Mock Scope serving image frames on :4800 (tele) and :4804 (wide)."""

import socket

import pytest

from seestar_controller.link import image
from seestar_controller.link.mock_scope import (
    FRAME_MAGIC,
    MockScope,
    make_frame_payload,
    make_jpeg_payload,
)


def _read_frames(port: int, count: int):
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        frames = []
        for _ in range(count):
            frame = image.read_frame(sock)
            assert frame is not None
            frames.append(frame)
        return frames, sock
    except Exception:
        sock.close()
        raise


def test_tele_socket_serves_frames(keypair):
    _private_pem, public_pem = keypair
    scope = MockScope(
        "127.0.0.1", 0, 0, 0,
        public_pem=public_pem,
        frame_count=5,
        frame_interval=0.01,
        frame_size=512,
        tele_resolution=(1920, 1080),
    )
    scope.start()
    try:
        frames, sock = _read_frames(scope.tele_port, 5)
        try:
            for seq, frame in enumerate(frames):
                assert frame.code == 0
                assert frame.id == seq
                assert (frame.width, frame.height) == (1920, 1080)
                assert frame.size == 512
                assert frame.payload == make_frame_payload(seq, 512)
                assert frame.payload.startswith(FRAME_MAGIC)
            # after frame_count frames the mock closes the image socket
            assert image.read_frame(sock) is None
        finally:
            sock.close()
    finally:
        scope.stop()


def test_wide_socket_serves_frames_with_wide_resolution(keypair):
    _private_pem, public_pem = keypair
    scope = MockScope(
        "127.0.0.1", 0, 0, 0,
        public_pem=public_pem,
        frame_count=3,
        frame_interval=0.01,
        frame_size=256,
        wide_resolution=(1280, 720),
    )
    scope.start()
    try:
        frames, sock = _read_frames(scope.wide_port, 3)
        try:
            assert [(f.width, f.height) for f in frames] == [(1280, 720)] * 3
            assert [f.id for f in frames] == [0, 1, 2]
        finally:
            sock.close()
    finally:
        scope.stop()


def test_default_ports_are_4700_4800_4804():
    """The mock's defaults match the Scope's real ports."""
    MockScope.__new__(MockScope)  # inspect defaults without binding
    import inspect

    sig = inspect.signature(MockScope.__init__)
    assert sig.parameters["command_port"].default == 4700
    assert sig.parameters["tele_port"].default == image.TELE_PORT == 4800
    assert sig.parameters["wide_port"].default == image.WIDE_PORT == 4804


def test_mock_rejects_bad_signature(keypair):
    """A verify_client with a signature from the wrong key is rejected."""
    import time

    from seestar_controller.link import auth, protocol

    _private_pem, public_pem = keypair
    wrong_private_pem, _ = auth.generate_keypair()
    scope = MockScope("127.0.0.1", 0, 0, 0, public_pem=public_pem)
    scope.start()
    try:
        sock = socket.create_connection(("127.0.0.1", scope.command_port), timeout=2)
        try:
            decoder = protocol.MessageDecoder()

            def rpc(msg):
                sock.sendall(protocol.encode_message(msg))
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    data = sock.recv(65536)
                    if not data:
                        raise AssertionError("socket closed")
                    for m in decoder.feed(data):
                        d = protocol.classify(m)
                        if isinstance(d, protocol.Reply) and d.id == msg["id"]:
                            return d
                raise AssertionError("no reply")

            challenge = rpc({"id": 1, "method": auth.AUTH_CHALLENGE_METHOD}).result["str"]
            wrong_key = auth.load_private_key(wrong_private_pem)
            sign = auth.sign_challenge(wrong_key, challenge)
            reply = rpc(
                {
                    "id": 2,
                    "method": auth.AUTH_VERIFY_METHOD,
                    "params": {"sign": sign, "data": challenge},
                }
            )
            assert reply.code != 0
            status = rpc({"id": 3, "method": auth.AUTH_STATUS_METHOD})
            assert status.result is False
        finally:
            sock.close()
    finally:
        scope.stop()


def test_get_scope_state_reports_mode_target_and_stacking(transport):
    """Wire shape of get_scope_state: mode, target, stacking progress."""
    result = transport.request("get_scope_state")
    assert result["mode"] == "idle"
    assert result["target"] is None
    assert result["stacking"] == {"state": "idle", "lapse_ms": 0, "frames": 0}


def test_goto_then_state_and_arrival_event(transport, mock_scope):
    """scope_goto_target accepts the slew, simulates its duration, then updates
    the state and pushes a ScopeGoto "complete" event."""
    import threading

    from seestar_controller.link import protocol

    mock_scope.slew_duration = 0.05
    events: list[protocol.Event] = []
    done = threading.Event()
    transport.add_event_handler(
        "ScopeGoto",
        lambda e: (events.append(e), done.set() if e.payload.get("state") == "complete" else None),
    )
    result = transport.request("scope_goto_target", {"ra": 1.5, "dec": 2.5})
    assert result == {"ra": 1.5, "dec": 2.5}
    # during the slew the state shows slewing with the target set
    mid = mock_scope.scope_state()
    assert mid["mode"] == "slewing"
    assert mid["target"] == {"ra": 1.5, "dec": 2.5}
    assert done.wait(timeout=3), "no ScopeGoto complete event"
    final = mock_scope.scope_state()
    assert final["mode"] == "idle"
    assert final["target"] == {"ra": 1.5, "dec": 2.5}
    assert mock_scope.last_goto == {"ra": 1.5, "dec": 2.5}


def test_park_sets_mode_parked_and_pushes_event(transport, mock_scope):
    import threading

    from seestar_controller.link import protocol

    events: list[protocol.Event] = []
    done = threading.Event()
    transport.add_event_handler(
        "ScopePark", lambda e: (events.append(e), done.set())
    )
    result = transport.request("scope_park")
    assert result == {"mode": "parked"}
    assert mock_scope.scope_state()["mode"] == "parked"
    assert done.wait(timeout=3)
    assert events[0].payload["state"] == "parked"


def test_mock_skips_malformed_json_line_and_keeps_serving(keypair):
    """A garbage line on the command channel must not kill the mock's client
    handler: the bad line is skipped and later valid requests still get replies."""
    import time

    from seestar_controller.link import protocol

    _private_pem, public_pem = keypair
    scope = MockScope("127.0.0.1", 0, 0, 0, public_pem=public_pem)
    scope.start()
    try:
        sock = socket.create_connection(("127.0.0.1", scope.command_port), timeout=2)
        try:
            decoder = protocol.MessageDecoder()

            def rpc(msg):
                sock.sendall(protocol.encode_message(msg))
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    data = sock.recv(65536)
                    if not data:
                        raise AssertionError("socket closed")
                    for m in decoder.feed(data):
                        d = protocol.classify(m)
                        if isinstance(d, protocol.Reply) and d.id == msg["id"]:
                            return d
                raise AssertionError("no reply")

            # garbage line and a valid request in the SAME tcp chunk: the mock
            # must skip the garbage and still answer the valid request
            sock.sendall(
                b"this is not json\r\n"
                + protocol.encode_message({"id": 1, "method": "pi_is_verified"})
            )
            deadline = time.monotonic() + 3
            reply = None
            while reply is None and time.monotonic() < deadline:
                data = sock.recv(65536)
                if not data:
                    raise AssertionError("socket closed after garbage line")
                for m in decoder.feed(data):
                    d = protocol.classify(m)
                    if isinstance(d, protocol.Reply) and d.id == 1:
                        reply = d
            assert reply is not None, "mock died on malformed line"
            assert reply.code == 0
            # and it keeps serving afterwards
            assert rpc({"id": 2, "method": "pi_is_verified"}).code == 0
            assert scope.client_count == 1
        finally:
            sock.close()
    finally:
        scope.stop()


# -- preview streaming knobs ------------------------------------------------------


def test_make_jpeg_payload_is_a_jpeg_with_counter():
    from seestar_controller.link.preview import decode_payload, is_jpeg

    payload = make_jpeg_payload(9, 64, 48)
    assert is_jpeg(payload)
    assert not is_jpeg(make_frame_payload(9, 64))
    frame = image.Frame(
        code=0, id=9, width=64, height=48, size=len(payload), payload=payload
    )
    arr = decode_payload(frame)
    assert arr.shape == (48, 64, 3)
    assert abs(float(arr[..., 2].mean()) - 9) <= 2  # counter in blue channel


def test_jpeg_frames_served_on_image_socket(keypair):
    _private_pem, public_pem = keypair
    scope = MockScope(
        "127.0.0.1", 0, 0, 0,
        public_pem=public_pem,
        frame_format="jpeg",
        frame_count=3,
        frame_interval=0.01,
        tele_resolution=(64, 48),
    )
    scope.start()
    try:
        sock = socket.create_connection(("127.0.0.1", scope.tele_port), timeout=5)
        try:
            for seq in range(3):
                frame = image.read_frame(sock)
                assert frame is not None
                assert frame.id == seq
                assert (frame.width, frame.height) == (64, 48)
                assert frame.payload[:2] == b"\xff\xd8"  # JPEG SOI
            # after frame_count frames the mock closes the image socket
            assert image.read_frame(sock) is None
        finally:
            sock.close()
    finally:
        scope.stop()


def test_chunked_frame_delivery_is_reassembled_by_read_frame(keypair):
    """chunk_size splits each frame into small TCP segments; read_frame must
    still reassemble header + payload."""
    _private_pem, public_pem = keypair
    scope = MockScope(
        "127.0.0.1", 0, 0, 0,
        public_pem=public_pem,
        frame_count=3,
        frame_interval=0.01,
        frame_size=100,
        chunk_size=7,
    )
    scope.start()
    try:
        sock = socket.create_connection(("127.0.0.1", scope.tele_port), timeout=5)
        try:
            for seq in range(3):
                frame = image.read_frame(sock)
                assert frame is not None
                assert frame.id == seq
                assert frame.payload == make_frame_payload(seq, 100)
        finally:
            sock.close()
    finally:
        scope.stop()


def test_stop_start_streaming_gates_frame_push(keypair):
    _private_pem, public_pem = keypair
    scope = MockScope(
        "127.0.0.1", 0, 0, 0,
        public_pem=public_pem,
        frame_count=None,
        frame_interval=0.01,
        frame_size=64,
    )
    scope.start()
    try:
        sock = socket.create_connection(("127.0.0.1", scope.tele_port), timeout=5)
        try:
            assert image.read_frame(sock).id == 0  # ty: ignore[missing-union-attribute]
            assert image.read_frame(sock).id == 1  # ty: ignore[missing-union-attribute]
            scope.stop_streaming()
            assert not scope.streaming
            sock.settimeout(0.4)
            with pytest.raises(socket.timeout):
                image.read_frame(sock)  # nothing pushed while paused
            scope.start_streaming()
            assert scope.streaming
            sock.settimeout(5)
            assert image.read_frame(sock).id == 2
        finally:
            sock.close()
    finally:
        scope.stop()


def test_stream_fps_property_sets_frame_interval():
    scope = MockScope.__new__(MockScope)  # property test without binding
    scope.frame_interval = 0.05
    assert scope.stream_fps == 20.0
    scope.stream_fps = 50
    assert scope.frame_interval == pytest.approx(0.02)
    assert scope.stream_fps == 50.0
    with pytest.raises(ValueError):
        scope.stream_fps = 0


def test_invalid_frame_format_and_chunk_size_rejected():
    with pytest.raises(ValueError, match="frame_format"):
        MockScope("127.0.0.1", 0, 0, 0, frame_format="png")
    with pytest.raises(ValueError, match="chunk_size"):
        MockScope("127.0.0.1", 0, 0, 0, chunk_size=0)
