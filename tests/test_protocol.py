"""Envelope encode/decode and message classification (no sockets)."""

import json

import pytest

from seestar_controller.link.protocol import (
    Event,
    MessageDecoder,
    Reply,
    Request,
    classify,
    encode_message,
)


def test_encode_message_appends_crlf():
    encoded = encode_message({"id": 1, "method": "test_connection"})
    assert encoded == b'{"id": 1, "method": "test_connection"}\r\n'
    assert json.loads(encoded.decode().strip()) == {"id": 1, "method": "test_connection"}


def test_decoder_roundtrip_single_message():
    decoder = MessageDecoder()
    messages = decoder.feed(encode_message({"id": 7, "method": "get_device_state"}))
    assert messages == [{"id": 7, "method": "get_device_state"}]
    assert decoder.pending_bytes == 0


def test_decoder_reassembles_message_split_across_chunks():
    decoder = MessageDecoder()
    blob = encode_message({"id": 3, "method": "scope_get_equ_coord", "params": {"x": 1}})
    out = []
    for i in range(0, len(blob), 3):  # drip-feed 3 bytes at a time
        out.extend(decoder.feed(blob[i : i + 3]))
    assert out == [{"id": 3, "method": "scope_get_equ_coord", "params": {"x": 1}}]


def test_decoder_multiple_messages_in_one_buffer():
    decoder = MessageDecoder()
    blob = encode_message({"id": 1, "method": "a"}) + encode_message({"id": 2, "method": "b"})
    messages = decoder.feed(blob)
    assert [m["id"] for m in messages] == [1, 2]


def test_decoder_skips_blank_lines():
    decoder = MessageDecoder()
    messages = decoder.feed(b"\r\n" + encode_message({"id": 9, "method": "x"}) + b"\r\n")
    assert messages == [{"id": 9, "method": "x"}]


def test_decoder_keeps_partial_line_buffered():
    decoder = MessageDecoder()
    assert decoder.feed(b'{"id": 1, "met') == []
    assert decoder.pending_bytes == len(b'{"id": 1, "met')
    assert decoder.feed(b'hod": "x"}\r\n') == [{"id": 1, "method": "x"}]


def test_decoder_invalid_json_raises():
    decoder = MessageDecoder()
    with pytest.raises(json.JSONDecodeError):
        decoder.feed(b"not json\r\n")


def test_classify_request():
    decoded = classify({"id": 112, "method": "iscope_start_view", "params": {"mode": "star"}})
    assert isinstance(decoded, Request)
    assert decoded.id == 112
    assert decoded.method == "iscope_start_view"
    assert decoded.params == {"mode": "star"}
    assert decoded.to_dict() == {
        "id": 112,
        "method": "iscope_start_view",
        "params": {"mode": "star"},
    }


def test_request_to_dict_keeps_positional_params_a_list():
    # fw 9.75 scope_goto: {"id": 113, "method": "scope_goto", "params": [13.4, 54.8]}
    decoded = classify({"id": 113, "method": "scope_goto", "params": [13.4, 54.8]})
    assert isinstance(decoded, Request)
    assert decoded.params == [13.4, 54.8]
    assert decoded.to_dict() == {
        "id": 113,
        "method": "scope_goto",
        "params": [13.4, 54.8],
    }


def test_classify_reply():
    raw = {
        "jsonrpc": "2.0",
        "Timestamp": "9507.244805160",
        "method": "scope_get_equ_coord",
        "result": {"ra": 17.093056, "dec": 34.349722},
        "code": 0,
        "id": 83,
    }
    decoded = classify(raw)
    assert isinstance(decoded, Reply)
    assert decoded.id == 83
    assert decoded.code == 0
    assert decoded.ok
    assert decoded.result == {"ra": 17.093056, "dec": 34.349722}
    assert decoded.timestamp == "9507.244805160"


def test_classify_reply_error_code_not_ok():
    decoded = classify({"jsonrpc": "2.0", "method": "m", "code": 207, "id": 5})
    assert isinstance(decoded, Reply)
    assert not decoded.ok


def test_classify_event():
    raw = {
        "Event": "EqModePA",
        "Timestamp": "740.411562378",
        "state": "working",
        "lapse_ms": 0,
        "route": [],
    }
    decoded = classify(raw)
    assert isinstance(decoded, Event)
    assert decoded.name == "EqModePA"
    assert decoded.payload["state"] == "working"


def test_request_without_params_omits_params_key():
    assert Request(id=2, method="test_connection").to_dict() == {
        "id": 2,
        "method": "test_connection",
    }
