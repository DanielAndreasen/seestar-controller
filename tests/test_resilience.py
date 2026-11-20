"""Resilience (ticket 07): reconnect with backoff, unified lifecycle event
dispatch, and the rotating protocol log — all against the mock Scope."""

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from seestar_controller.link.device import Device
from seestar_controller.link.mock_scope import MockScope
from seestar_controller.link.protocol_log import (
    DEFAULT_LOG_PATH,
    LOG_ENABLED_ENV_VAR,
    LOG_PATH_ENV_VAR,
    ProtocolLog,
    log_enabled,
    redact,
    resolve_log_path,
    resolve_protocol_log,
)
from seestar_controller.link.resilience import (
    CONNECTION_LOST_EVENT,
    LIFECYCLE_EVENTS,
    RECONNECTED_EVENT,
    RECONNECTING_EVENT,
    SCOPE_ERROR_EVENT,
    STACK_FINISHED_EVENT,
    ConnectionState,
    ReconnectPolicy,
)
from seestar_controller.link.transport import (
    ConnectionLostError,
    LinkTransport,
    RpcError,
)

#: Fast reconnect policy for tests: backoff 0.05 -> 0.1 -> 0.2 (capped).
FAST_POLICY = ReconnectPolicy(base_delay=0.05, cap=0.2)


def build_transport(mock_scope, keypair, reconnect, **kwargs) -> LinkTransport:
    """A connected LinkTransport to the mock Scope with the given reconnect policy."""
    private_pem, _public_pem = keypair
    t = LinkTransport(
        "127.0.0.1",
        mock_scope.command_port,
        pem=private_pem,
        heartbeat_interval=0.1,
        heartbeat_timeout=0.5,
        request_timeout=2.0,
        reconnect=reconnect,
        **kwargs,
    )
    t.connect()
    return t


def read_log(path: Path) -> list[dict]:
    """Parse every JSON line of a protocol log file."""
    records = []
    for line in Path(path).read_text().splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def collect(names) -> tuple[dict, dict]:
    """Handler factory: count events per name and keep their payloads."""
    counts = {name: 0 for name in names}
    payloads: dict[str, list[dict]] = {name: [] for name in names}

    def make(name):
        def handler(event):
            counts[name] += 1
            payloads[name].append(dict(event.payload))
        return handler

    return counts, payloads, make


# -- ReconnectPolicy ---------------------------------------------------------


def test_reconnect_policy_exponential_backoff_capped():
    policy = ReconnectPolicy(max_attempts=5, base_delay=0.5, cap=2.0)
    assert policy.delay_for(1) == 0.5
    assert policy.delay_for(2) == 1.0
    assert policy.delay_for(3) == 2.0
    assert policy.delay_for(4) == 2.0  # capped
    assert policy.delay_for(99) == 2.0
    with pytest.raises(ValueError):
        policy.delay_for(0)


def test_reconnect_policy_validation():
    with pytest.raises(ValueError):
        ReconnectPolicy(base_delay=0)
    with pytest.raises(ValueError):
        ReconnectPolicy(cap=-1.0)
    with pytest.raises(ValueError):
        ReconnectPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        ReconnectPolicy(factor=0.5)
    # defaults: unlimited attempts
    assert ReconnectPolicy().max_attempts is None


# -- connection state ----------------------------------------------------------


def test_connection_state_surfaced_to_callers(mock_scope, keypair):
    t = LinkTransport(
        "127.0.0.1", mock_scope.command_port, pem=keypair[0],
        heartbeat_interval=0.1, request_timeout=2.0,
    )
    assert t.connection_state == ConnectionState.DISCONNECTED
    t.connect()
    assert t.connection_state == ConnectionState.CONNECTED
    t.disconnect()
    assert t.connection_state == ConnectionState.DISCONNECTED


def test_device_surfaces_connection_state_and_default_reconnect(mock_scope, keypair):
    private_pem, _ = keypair
    d = Device.connect(
        "127.0.0.1", mock_scope.command_port, pem=private_pem,
        heartbeat_interval=0.1, request_timeout=2.0,
    )
    try:
        assert d.connection_state == ConnectionState.CONNECTED
        # the facade is resilient by default
        assert d.transport._reconnect_policy is not None
    finally:
        d.disconnect()
    assert d.connection_state == ConnectionState.DISCONNECTED
    # opt out explicitly
    d2 = Device.connect(
        "127.0.0.1", mock_scope.command_port, pem=private_pem,
        heartbeat_interval=0.1, request_timeout=2.0, reconnect=None,
    )
    try:
        assert d2.transport._reconnect_policy is None
    finally:
        d2.disconnect()


# -- reconnect -----------------------------------------------------------------


def test_drop_connection_triggers_reconnect_rehandshake_and_restored_subscriptions(
    mock_scope, keypair,
):
    t = build_transport(mock_scope, keypair, FAST_POLICY)
    counts, payloads, make = collect(
        (CONNECTION_LOST_EVENT, RECONNECTING_EVENT, RECONNECTED_EVENT)
    )
    for name, handler in ((n, make(n)) for n in counts):
        t.add_event_handler(name, handler)
    legacy_lost = []
    t.add_connection_lost_handler(lambda: legacy_lost.append(1))
    wildcard = []
    t.add_event_handler("*", lambda e: wildcard.append(e.name))
    sub_received = []
    t.add_event_handler("Test", lambda e: sub_received.append(e))
    assert "Test" in t.subscriptions

    mock_scope.drop_connections()

    assert wait_until(lambda: counts[RECONNECTED_EVENT] == 1), (
        "client did not reconnect after the mock dropped the connection"
    )
    # each lifecycle event dispatched exactly once per occurrence
    assert counts[CONNECTION_LOST_EVENT] == 1
    assert counts[RECONNECTING_EVENT] == 1
    assert counts[RECONNECTED_EVENT] == 1
    assert payloads[CONNECTION_LOST_EVENT][0]["reason"] == "socket eof"
    assert payloads[RECONNECTED_EVENT][0]["attempts"] >= 1
    assert len(legacy_lost) == 1  # legacy handler fired for the same occurrence
    assert CONNECTION_LOST_EVENT in wildcard and RECONNECTED_EVENT in wildcard
    # state surfaced to callers
    assert t.connected and t.verified
    assert t.connection_state == ConnectionState.CONNECTED
    # the signed handshake re-ran: the mock gates commands behind auth, so a
    # request only succeeds on the fresh, re-verified connection
    assert t.request("get_device_state")["fw_version"] == "9.75"
    assert wait_until(lambda: mock_scope.client_count == 1)
    # subscriptions restored: pushed events still reach the handler
    mock_scope.push_event({"Event": "Test", "value": 42})
    assert wait_until(lambda: bool(sub_received))
    assert sub_received[0].payload["value"] == 42
    t.disconnect()


def test_reconnect_backoff_gives_up_after_max_attempts(mock_scope, keypair):
    policy = ReconnectPolicy(max_attempts=3, base_delay=0.05, cap=0.1)
    t = build_transport(mock_scope, keypair, policy)
    counts, _payloads, make = collect(
        (CONNECTION_LOST_EVENT, RECONNECTING_EVENT, RECONNECTED_EVENT)
    )
    for name in counts:
        t.add_event_handler(name, make(name))

    start = time.monotonic()
    mock_scope.stop()  # the port is now closed: reconnect attempts are refused
    assert wait_until(
        lambda: t.connection_state == ConnectionState.DISCONNECTED, timeout=5
    ), "transport did not give up after max_attempts"
    elapsed = time.monotonic() - start

    assert not t.connected
    assert t.reconnect_attempts == 3
    assert counts[CONNECTION_LOST_EVENT] == 1
    assert counts[RECONNECTING_EVENT] == 1
    assert counts[RECONNECTED_EVENT] == 0
    # exponential backoff actually waited: 0.05 + 0.1 + 0.1 (capped) before giving up
    assert elapsed >= 0.2
    t.disconnect()


def test_reconnect_after_scope_restart_on_same_port(mock_scope, keypair):
    """Kill the mock mid-session, restart it on the same port: the client
    reconnects automatically and re-runs the handshake."""
    t = build_transport(mock_scope, keypair, FAST_POLICY)
    reconnected = []
    t.add_event_handler(RECONNECTED_EVENT, lambda e: reconnected.append(e))

    port = mock_scope.command_port
    mock_scope.stop()
    scope2 = MockScope("127.0.0.1", port, 0, 0, public_pem=keypair[1])
    scope2.start()
    try:
        assert wait_until(lambda: bool(reconnected), timeout=5), (
            "client did not reconnect after the scope restarted"
        )
        assert t.connected and t.verified
        assert t.request("get_device_state")["fw_version"] == "9.75"
        assert wait_until(lambda: scope2.client_count == 1)
    finally:
        scope2.stop()
        t.disconnect()


def test_disconnect_stops_the_reconnect_loop(mock_scope, keypair):
    t = build_transport(mock_scope, keypair, FAST_POLICY)  # unlimited attempts
    reconnected = []
    t.add_event_handler(RECONNECTED_EVENT, lambda e: reconnected.append(e))
    mock_scope.drop_connections()
    assert wait_until(lambda: t.connection_state == ConnectionState.RECONNECTING)
    t.disconnect()
    assert t.connection_state == ConnectionState.DISCONNECTED
    thread = t._reconnect_thread
    assert wait_until(lambda: thread is None or not thread.is_alive(), timeout=2)
    time.sleep(0.3)  # the mock is still up: a live loop would have reconnected
    assert not reconnected


def test_drop_connections_hook_without_reconnect_policy(mock_scope, keypair):
    """Without a reconnect policy the drop is a plain connection loss."""
    t = build_transport(mock_scope, keypair, None)
    lost = threading.Event()
    t.add_connection_lost_handler(lost.set)
    mock_scope.drop_connections()
    assert lost.wait(timeout=5)
    assert wait_until(lambda: not t.connected)
    assert t.connection_state == ConnectionState.DISCONNECTED
    with pytest.raises(ConnectionLostError):
        t.request("get_device_state", timeout=0.5)
    t.disconnect()


# -- unified event/error dispatch ------------------------------------------------


def test_lifecycle_and_error_events_dispatched_exactly_once(mock_scope, keypair):
    private_pem, _ = keypair
    d = Device.connect(
        "127.0.0.1",
        mock_scope.command_port,
        pem=private_pem,
        heartbeat_interval=0.1,
        heartbeat_timeout=0.5,
        request_timeout=2.0,
        reconnect=FAST_POLICY,
    )
    counts, payloads, make = collect(LIFECYCLE_EVENTS)
    for name in LIFECYCLE_EVENTS:
        d.add_event_handler(name, make(name))
    try:
        # scope error: one non-zero reply -> exactly one scope_error event
        with pytest.raises(RpcError):
            d.transport.request("mock_fail")
        assert counts[SCOPE_ERROR_EVENT] == 1
        assert payloads[SCOPE_ERROR_EVENT][0]["code"] == 500
        assert payloads[SCOPE_ERROR_EVENT][0]["method"] == "mock_fail"
        with pytest.raises(RpcError):
            d.transport.request("mock_fail")
        assert counts[SCOPE_ERROR_EVENT] == 2  # once per occurrence

        # stack finished: one final Stack event -> exactly one stack_finished
        d.start_stack(10.0, 80)
        d.stop_stack()
        assert wait_until(lambda: counts[STACK_FINISHED_EVENT] == 1, timeout=3)
        assert payloads[STACK_FINISHED_EVENT][0]["state"] == "idle"
        assert "frames" in payloads[STACK_FINISHED_EVENT][0]

        # drop + reconnect: connection_lost / reconnecting / reconnected once each
        mock_scope.drop_connections()
        assert wait_until(lambda: counts[RECONNECTED_EVENT] == 1, timeout=5)
        assert counts[CONNECTION_LOST_EVENT] == 1
        assert counts[RECONNECTING_EVENT] == 1
        assert counts[RECONNECTED_EVENT] == 1
        assert d.connection_state == ConnectionState.CONNECTED

        # a second stack cycle after the reconnect -> stack_finished again
        d.start_stack(10.0, 80)
        d.stop_stack()
        assert wait_until(lambda: counts[STACK_FINISHED_EVENT] == 2, timeout=3)
        # the reconnect handshake succeeded: no extra scope errors
        assert counts[SCOPE_ERROR_EVENT] == 2
    finally:
        d.disconnect()


# -- protocol log ----------------------------------------------------------------


def test_protocol_log_records_traffic_with_direction_timestamp_and_kind(
    mock_scope, keypair, tmp_path,
):
    log_path = tmp_path / "protocol.log"
    t = build_transport(mock_scope, keypair, None, protocol_log=ProtocolLog(log_path))
    t.request("get_device_state")
    mock_scope.push_event({"Event": "Test", "x": 1})
    assert wait_until(
        lambda: any(
            r["dir"] == "rx" and r["kind"] == "event" for r in read_log(log_path)
        ),
        timeout=3,
    )
    t.disconnect()

    records = read_log(log_path)
    assert records
    for record in records:
        assert set(record) >= {"ts", "dir", "kind", "msg"}
        datetime.fromisoformat(record["ts"])  # parseable UTC timestamp
        assert record["dir"] in {"tx", "rx", "local"}
    tx_requests = [r for r in records if r["dir"] == "tx" and r["kind"] == "request"]
    rx_replies = [r for r in records if r["dir"] == "rx" and r["kind"] == "reply"]
    rx_events = [r for r in records if r["dir"] == "rx" and r["kind"] == "event"]
    assert any(r["msg"].get("method") == "get_device_state" for r in tx_requests)
    assert any(
        r["msg"].get("method") == "get_device_state" and r["msg"].get("code") == 0
        for r in rx_replies
    )
    assert any(r["msg"].get("Event") == "Test" for r in rx_events)
    # the signed handshake traffic is logged too
    assert any(r["msg"].get("method") == "get_verify_str" for r in tx_requests)
    assert any(r["msg"].get("method") == "verify_client" for r in tx_requests)


def test_protocol_log_redacts_signatures_and_pem(mock_scope, keypair, tmp_path):
    log_path = tmp_path / "protocol.log"
    private_pem, _public_pem = keypair
    t = build_transport(mock_scope, keypair, None, protocol_log=ProtocolLog(log_path))
    t.request("get_device_state")
    t.disconnect()

    content = log_path.read_text()
    assert private_pem.decode() not in content
    assert "PRIVATE KEY-----" not in content
    records = read_log(log_path)
    verify = [
        r for r in records
        if r["dir"] == "tx" and r["msg"].get("method") == "verify_client"
    ]
    assert verify, "handshake verify_client request not logged"
    for record in verify:
        assert record["msg"]["params"]["sign"] == "[REDACTED]"
        assert record["msg"]["params"]["data"]  # the challenge is not key material

    def values_for_key(obj, key):
        found = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                if str(k).lower() == key:
                    found.append(v)
                found.extend(values_for_key(v, key))
        elif isinstance(obj, list):
            for item in obj:
                found.extend(values_for_key(item, key))
        return found

    for record in records:
        assert values_for_key(record["msg"], "sign") == ["[REDACTED]"] * len(
            values_for_key(record["msg"], "sign")
        )
        assert not values_for_key(record["msg"], "pem")


def test_redact_unit():
    msg = {
        "id": 1,
        "method": "verify_client",
        "params": {
            "sign": "abc123",
            "data": "challenge",
            "nested": {
                "SIGNATURE": "xyz",
                "pem": "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----",
            },
            "note": "key material: -----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----",
        },
    }
    out = redact(msg)
    assert out["params"]["sign"] == "[REDACTED]"
    assert out["params"]["data"] == "challenge"
    assert out["params"]["nested"]["SIGNATURE"] == "[REDACTED]"
    assert out["params"]["nested"]["pem"] == "[REDACTED]"
    assert out["params"]["note"] == "key material: [REDACTED PEM]"
    assert out["id"] == 1
    # the original is untouched
    assert msg["params"]["sign"] == "abc123"


def test_protocol_log_rotation(tmp_path):
    log_path = tmp_path / "protocol.log"
    log = ProtocolLog(log_path, max_bytes=500, backup_count=2)
    for i in range(60):
        log.log("tx", "request", {"id": i, "method": "m", "params": {"pad": "x" * 40}})
    log.close()
    backups = sorted(p for p in tmp_path.iterdir() if p.name.startswith("protocol.log."))
    assert backups, "no rotation backups were created"
    # the current file stays bounded (one record of slack)
    assert os.path.getsize(log_path) <= 500 + 200
    # every line in every file is one JSON record
    for path in [log_path, *backups]:
        for line in path.read_text().splitlines():
            if line.strip():
                record = json.loads(line)
                assert record["dir"] == "tx" and record["kind"] == "request"


def test_protocol_log_toggle_via_flag_and_env(mock_scope, keypair, tmp_path, monkeypatch):
    # explicit flag wins over env
    assert resolve_protocol_log(False) is None
    assert resolve_protocol_log(False, env={LOG_ENABLED_ENV_VAR: "1"}) is None
    # env var toggles
    assert resolve_protocol_log(None, env={LOG_ENABLED_ENV_VAR: "0"}) is None
    log = resolve_protocol_log(
        None,
        env={LOG_ENABLED_ENV_VAR: "1", LOG_PATH_ENV_VAR: str(tmp_path / "env.log")},
    )
    assert isinstance(log, ProtocolLog)
    assert log.path == tmp_path / "env.log"
    log.close()
    # default: ON, under the config dir (pure resolution — no file created)
    assert log_enabled(None, env={}) is True
    assert resolve_log_path(None, env={}) == DEFAULT_LOG_PATH
    assert resolve_log_path(None, env={LOG_PATH_ENV_VAR: "/tmp/x.log"}) == Path("/tmp/x.log")
    assert resolve_log_path("/tmp/y.log", env={}) == Path("/tmp/y.log")
    with pytest.raises(ValueError):
        log_enabled(None, env={LOG_ENABLED_ENV_VAR: "maybe"})

    # transport wiring: disabled -> no log object, nothing written
    t = build_transport(mock_scope, keypair, None, protocol_log=False)
    assert t.protocol_log is None
    t.request("get_device_state")
    t.disconnect()

    # transport wiring: explicit path
    explicit = tmp_path / "explicit.log"
    t = build_transport(
        mock_scope, keypair, None, protocol_log=True, protocol_log_path=explicit
    )
    assert t.protocol_log is not None and t.protocol_log.path == explicit
    t.request("get_device_state")
    t.disconnect()
    assert explicit.exists() and read_log(explicit)

    # transport wiring: env-driven (conftest pins SEESTAR_PROTOCOL_LOG=0)
    monkeypatch.setenv(LOG_ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(LOG_PATH_ENV_VAR, str(tmp_path / "env-driven.log"))
    t = build_transport(mock_scope, keypair, None)
    assert t.protocol_log is not None
    assert t.protocol_log.path == tmp_path / "env-driven.log"
    t.request("get_device_state")
    t.disconnect()
    assert (tmp_path / "env-driven.log").exists()


def test_protocol_log_shows_the_reconnect_gap(mock_scope, keypair, tmp_path):
    """The log makes a dropped link visible: lifecycle lines plus a second
    handshake after the reconnect."""
    log_path = tmp_path / "protocol.log"
    t = build_transport(
        mock_scope, keypair, FAST_POLICY, protocol_log=ProtocolLog(log_path)
    )
    t.request("get_device_state")
    reconnected = []
    t.add_event_handler(RECONNECTED_EVENT, lambda e: reconnected.append(e))
    mock_scope.drop_connections()
    assert wait_until(lambda: bool(reconnected), timeout=5)
    assert t.connection_state == ConnectionState.CONNECTED
    t.request("get_device_state")
    t.disconnect()

    records = read_log(log_path)
    local_names = [r["msg"]["Event"] for r in records if r["dir"] == "local"]
    assert CONNECTION_LOST_EVENT in local_names
    assert RECONNECTING_EVENT in local_names
    assert RECONNECTED_EVENT in local_names
    # initial handshake + the re-run handshake after the reconnect
    challenges = [
        r for r in records
        if r["dir"] == "tx" and r["msg"].get("method") == "get_verify_str"
    ]
    assert len(challenges) == 2
