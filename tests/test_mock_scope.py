"""Mock Scope serving image frames on :4800 (tele) and :4804 (wide)."""

import socket

from seestar_controller.link import image
from seestar_controller.link.mock_scope import FRAME_MAGIC, MockScope, make_frame_payload


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
    scope = MockScope.__new__(MockScope)  # inspect defaults without binding
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
