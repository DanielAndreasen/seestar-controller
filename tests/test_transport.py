"""Client transport against the mock Scope: signed connect, correlation, events, heartbeat."""

import socket
import threading
import time

import pytest

from seestar_controller.link import auth, protocol
from seestar_controller.link.auth import HandshakeError
from seestar_controller.link.mock_scope import NOT_VERIFIED_CODE, MockScope
from seestar_controller.link.transport import (
    ConnectionLostError,
    LinkTransport,
    RpcError,
)


def test_connect_signed_handshake_succeeds(transport, mock_scope):
    assert transport.connected
    assert transport.verified
    assert mock_scope.client_count == 1


def test_request_reply_roundtrip(transport):
    result = transport.request("get_device_state")
    assert result["fw_version"] == "9.75"
    assert result["mode"] == "idle"


def test_request_echo_params_correlate(transport):
    result = transport.request("some_method", {"n": 5})
    assert result["echo_method"] == "some_method"
    assert result["echo_params"] == {"n": 5}


def test_concurrent_requests_correlated_by_id(transport, mock_scope):
    """Many in-flight requests; replies may arrive out of order, each caller
    must get its own reply (matched by id, not by arrival order)."""
    mock_scope.reply_delay["slow_method"] = 0.3  # forces out-of-order replies
    results: dict[int, object] = {}
    errors: list[Exception] = []

    def call(n: int) -> None:
        try:
            method = "slow_method" if n == 0 else "fast_method"
            results[n] = transport.request(method, {"n": n})
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)

    threads = [threading.Thread(target=call, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not errors
    assert len(results) == 8
    for n, result in results.items():
        assert result["echo_params"] == {"n": n}


def test_event_dispatch_to_registered_handler(transport, mock_scope):
    received: list[protocol.Event] = []
    done = threading.Event()

    def handler(event: protocol.Event) -> None:
        received.append(event)
        done.set()

    transport.add_event_handler("Stack", handler)
    mock_scope.push_event({"Event": "Stack", "state": "working", "lapse_ms": 0})
    assert done.wait(timeout=3)
    assert received[0].name == "Stack"
    assert received[0].payload["state"] == "working"


def test_event_via_rpc_trigger(transport):
    received: list[protocol.Event] = []
    done = threading.Event()
    transport.add_event_handler("Test", lambda e: (received.append(e), done.set()))
    transport.request("mock_push_event", {"Event": "Test", "value": 1})
    assert done.wait(timeout=3)
    assert received[0].payload["value"] == 1


def test_wildcard_event_handler(transport, mock_scope):
    received: list[str] = []
    done = threading.Event()
    transport.add_event_handler("*", lambda e: (received.append(e.name), done.set()))
    mock_scope.push_event({"Event": "Anything"})
    assert done.wait(timeout=3)
    assert received == ["Anything"]


def test_heartbeat_keeps_connection_alive(transport, mock_scope):
    time.sleep(0.6)  # several heartbeat intervals at 0.1 s
    assert transport.connected
    assert mock_scope.heartbeat_count >= 3


def test_dropped_heartbeat_detected(transport, mock_scope):
    lost = threading.Event()
    transport.add_connection_lost_handler(lost.set)
    mock_scope.set_heartbeat_silent(True)
    assert lost.wait(timeout=5), "dropped heartbeat was not detected"
    deadline = time.monotonic() + 2
    while transport.connected and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not transport.connected


def test_request_after_connection_lost_raises(mock_scope, keypair):
    private_pem, _ = keypair
    t = LinkTransport(
        "127.0.0.1",
        mock_scope.command_port,
        pem=private_pem,
        heartbeat_interval=0.1,
        heartbeat_timeout=0.4,
        request_timeout=0.5,
    )
    t.connect()
    mock_scope.set_heartbeat_silent(True)
    deadline = time.monotonic() + 5
    while t.connected and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not t.connected
    with pytest.raises(ConnectionLostError):
        t.request("get_device_state", timeout=0.5)
    t.disconnect()


def test_handshake_with_wrong_key_rejected(mock_scope):
    wrong_private_pem, _ = auth.generate_keypair()
    t = LinkTransport("127.0.0.1", mock_scope.command_port, pem=wrong_private_pem)
    with pytest.raises(HandshakeError):
        t.connect()
    t.disconnect()


def test_command_before_handshake_rejected_by_mock(mock_scope):
    """The mock gates non-auth commands behind the signed handshake (fw >= 7.18)."""
    sock = socket.create_connection(("127.0.0.1", mock_scope.command_port), timeout=2)
    try:
        sock.sendall(protocol.encode_message({"id": 1, "method": "get_device_state"}))
        decoder = protocol.MessageDecoder()
        reply = None
        deadline = time.monotonic() + 3
        while reply is None and time.monotonic() < deadline:
            data = sock.recv(65536)
            if not data:
                break
            for message in decoder.feed(data):
                decoded = protocol.classify(message)
                if isinstance(decoded, protocol.Reply) and decoded.id == 1:
                    reply = decoded
        assert reply is not None
        assert reply.code == NOT_VERIFIED_CODE
    finally:
        sock.close()


def test_rpc_error_raised_for_nonzero_code(transport):
    with pytest.raises(RpcError) as excinfo:
        transport.request("mock_fail")
    assert excinfo.value.reply.code == 500
    assert excinfo.value.reply.result == {"error": "boom"}


def test_raw_handshake_then_command_over_socket(mock_scope, keypair):
    """Wire-level check: signed handshake unlocks gated commands (fw >= 7.18)."""
    private_pem, _ = keypair
    sock = socket.create_connection(("127.0.0.1", mock_scope.command_port), timeout=2)
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

        private_key = auth.load_private_key(private_pem)
        challenge = rpc({"id": 1, "method": auth.AUTH_CHALLENGE_METHOD}).result["str"]
        sign = auth.sign_challenge(private_key, challenge)
        ok = rpc(
            {
                "id": 2,
                "method": auth.AUTH_VERIFY_METHOD,
                "params": {"sign": sign, "data": challenge},
            }
        )
        assert ok.code == 0
        assert "key_envelope" in ok.result  # firmware 9.75 style reply
        verified = rpc({"id": 3, "method": auth.AUTH_STATUS_METHOD})
        assert verified.result is True
        good = rpc({"id": 4, "method": "get_device_state"})
        assert good.code == 0
        assert good.result["fw_version"] == "9.75"
    finally:
        sock.close()
