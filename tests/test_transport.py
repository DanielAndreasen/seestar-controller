"""Client transport against the mock Scope: signed connect, correlation, events, heartbeat."""

import socket
import threading
import time

import pytest

from seestar_controller.link import auth, protocol
from seestar_controller.link.auth import HandshakeError
from seestar_controller.link.mock_scope import NOT_VERIFIED_CODE
from seestar_controller.link.transport import (
    ConnectionLostError,
    LinkTransport,
    RequestTimeoutError,
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


def test_request_passes_list_params_through_unchanged(transport, mock_scope):
    """Positional params (fw 9.75 ``scope_goto`` sends ``[ra, dec]``) must go
    on the wire as a JSON array — ``dict(params)`` would mangle/crash on a
    list — while mappings still go as a JSON object."""
    reply = transport.request_raw("positional_method", [13.4, 54.8])
    assert reply.ok
    assert mock_scope.last_wire_params["positional_method"] == [13.4, 54.8]
    # a tuple is sent as an array too
    transport.request("tuple_method", (1.0, 2.0))
    assert mock_scope.last_wire_params["tuple_method"] == [1.0, 2.0]
    # mappings are unchanged
    transport.request("mapping_method", {"n": 5})
    assert mock_scope.last_wire_params["mapping_method"] == {"n": 5}
    # and the reply still correlates
    result = transport.request("positional_method", [0.5, -1.5])
    assert result["echo_params"] == {"args": [0.5, -1.5]}


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
        assert result["echo_params"] == {"n": n}  # ty: ignore[not-subscriptable]


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


def test_handshake_failure_leaks_nothing_and_transport_reusable(mock_scope, keypair):
    """A raising signed handshake must close the socket, stop the reader thread,
    reset state, and leave the transport able to connect again."""
    private_pem, _public_pem = keypair
    wrong_private_pem, _ = auth.generate_keypair()
    t = LinkTransport("127.0.0.1", mock_scope.command_port, pem=wrong_private_pem)
    with pytest.raises(HandshakeError):
        t.connect()
    assert not t.connected
    assert not t.verified
    assert t._sock is None
    assert t._reader_thread is None or not t._reader_thread.is_alive()
    # reusable: swap in the correct key and connect again on the same object
    t._private_key = auth.load_private_key(private_pem)
    t.connect()
    assert t.connected
    assert t.verified
    assert t.request("get_device_state")["fw_version"] == "9.75"
    assert mock_scope.client_count == 1
    t.disconnect()
    assert not t.connected


def test_malformed_line_from_scope_is_protocol_error(transport, mock_scope):
    """One garbage JSON line from the scope must not kill the reader thread
    silently: it is a protocol error -> connection-lost handling."""
    lost = threading.Event()
    transport.add_connection_lost_handler(lost.set)
    mock_scope.send_raw(b"this is not json\r\n")
    assert lost.wait(timeout=3), "malformed line did not trigger connection-lost"
    deadline = time.monotonic() + 2
    while transport.connected and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not transport.connected
    with pytest.raises(ConnectionLostError):
        transport.request("get_device_state", timeout=0.5)
    # the reader thread actually stopped (no zombie thread with connected=True)
    deadline = time.monotonic() + 2
    while (
        transport._reader_thread is not None
        and transport._reader_thread.is_alive()
        and time.monotonic() < deadline
    ):
        time.sleep(0.05)
    assert transport._reader_thread is None or not transport._reader_thread.is_alive()


@pytest.mark.slow
def test_busy_link_with_slow_replies_no_spurious_connection_lost(mock_scope, keypair):
    """Heartbeat replies may be slow/dropped on a busy-but-healthy link: liveness
    is last inbound activity, so pending requests must NOT fail with
    ConnectionLostError and the link must stay up."""
    private_pem, _ = keypair
    t = LinkTransport(
        "127.0.0.1",
        mock_scope.command_port,
        pem=private_pem,
        heartbeat_interval=0.05,
        heartbeat_timeout=0.8,
        request_timeout=3.0,
    )
    lost = threading.Event()
    t.add_connection_lost_handler(lost.set)
    t.connect()
    try:
        mock_scope.set_heartbeat_silent(True)  # heartbeat replies stop...
        mock_scope.reply_delay["slow"] = 0.15  # ...but the link stays busy
        results: dict[int, object] = {}
        errors: list[Exception] = []

        def call(n: int) -> None:
            try:
                results[n] = t.request("slow", {"n": n})
            except Exception as exc:  # noqa: BLE001 - collected for assertion
                errors.append(exc)

        threads = [threading.Thread(target=call, args=(n,)) for n in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5)
        # keep the link busy a while longer so heartbeats time out repeatedly
        deadline = time.monotonic() + 1.0
        n = 100
        while time.monotonic() < deadline:
            results[n] = t.request("slow", {"n": n})
            n += 1
        assert not errors
        assert len(results) == 8 + (n - 100)
        for r in results.values():
            assert r["echo_method"] == "slow"  # ty: ignore[not-subscriptable]
        assert not lost.is_set(), "busy-but-healthy link was declared lost"
        assert t.connected
    finally:
        t.disconnect()


def test_request_timeout_raises_request_timeout_error(mock_scope, keypair):
    """A per-request timeout raises the dedicated RequestTimeoutError (a
    TimeoutError), not ConnectionLostError; the link itself stays up."""
    private_pem, _ = keypair
    t = LinkTransport(
        "127.0.0.1",
        mock_scope.command_port,
        pem=private_pem,
        heartbeat_interval=0.1,
        heartbeat_timeout=5.0,  # >> request timeout: heartbeat must not interfere
        request_timeout=0.3,
    )
    t.connect()
    try:
        mock_scope.reply_delay["slow"] = 0.6  # slower than the request timeout
        with pytest.raises(RequestTimeoutError) as excinfo:
            t.request("slow", timeout=0.3)
        assert isinstance(excinfo.value, TimeoutError)
        assert not isinstance(excinfo.value, ConnectionLostError)
        # the link is still healthy: a later request succeeds
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                assert t.request("get_device_state")["fw_version"] == "9.75"
                break
            except RequestTimeoutError:
                time.sleep(0.05)
        else:
            raise AssertionError("link did not recover after a request timeout")
        assert t.connected
    finally:
        t.disconnect()


def test_lost_handler_can_call_disconnect_from_heartbeat_thread(mock_scope, keypair):
    """A connection-lost handler running on the heartbeat thread may call
    disconnect() without crashing (disconnect never joins the current thread,
    and handlers run outside _send_lock)."""
    private_pem, _ = keypair
    t = LinkTransport(
        "127.0.0.1",
        mock_scope.command_port,
        pem=private_pem,
        heartbeat_interval=0.05,
        heartbeat_timeout=0.3,
        request_timeout=1.0,
    )
    handler_done = threading.Event()
    errors: list[Exception] = []

    def handler() -> None:
        try:
            t.disconnect()
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)
        finally:
            handler_done.set()

    t.add_connection_lost_handler(handler)
    t.connect()
    mock_scope.set_heartbeat_silent(True)
    assert handler_done.wait(timeout=5), "connection-lost handler did not run"
    assert not errors, f"handler/disconnect crashed: {errors!r}"
    assert not t.connected
    deadline = time.monotonic() + 2
    while (
        t._heartbeat_thread is not None
        and t._heartbeat_thread.is_alive()
        and time.monotonic() < deadline
    ):
        time.sleep(0.05)
    assert t._heartbeat_thread is None or not t._heartbeat_thread.is_alive()
    t.disconnect()  # idempotent


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
