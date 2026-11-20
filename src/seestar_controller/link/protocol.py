"""Wire envelope for the Link's command channel (TCP :4700).

The Scope speaks newline-delimited JSON on the command socket: every
message is one JSON object terminated by ``\\r\\n``. Message shapes follow
the open-source seestar_alp / seestarpy projects:

- **Request** (client -> scope)::

    {"id": 112, "method": "iscope_start_view", "params": {"mode": "star"}}

  ``params`` may also be a JSON *array* for positional-param methods —
  fw 9.75 ``scope_goto`` takes ``{"id": 113, "method": "scope_goto",
  "params": [13.4, 54.8]}`` (``[ra_hours, dec_deg]``).

- **Reply** (scope -> client), correlated by ``id``::

    {"jsonrpc": "2.0", "Timestamp": "9507.244805160",
     "method": "scope_get_equ_coord",
     "result": {"ra": 17.093056, "dec": 34.349722}, "code": 0, "id": 83}

  ``code == 0`` means success; anything else is an RPC error.

- **Event** (scope -> client, pushed, no ``id``)::

    {"Event": "EqModePA", "Timestamp": "740.411562378",
     "state": "working", "lapse_ms": 0, "route": []}

Only the envelope is handled here; signing lives in
:mod:`seestar_controller.link.auth` and the socket plumbing in
:mod:`seestar_controller.link.transport`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: Line terminator used by the Scope on the command channel.
LINE_TERMINATOR = b"\r\n"

#: Method the Scope's spec recommends as keepalive on the command channel.
HEARTBEAT_METHOD = "test_connection"


@dataclass(frozen=True)
class Request:
    """A client -> scope request envelope.

    ``params`` is a mapping for object-style params, or a list for
    positional params (fw 9.75 ``scope_goto`` sends ``[ra, dec]``).
    """

    id: int
    method: str
    params: Mapping[str, Any] | Sequence[Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"id": self.id, "method": self.method}
        if self.params:
            # positional params stay a JSON array; mappings a JSON object
            msg["params"] = (
                list(self.params)
                if isinstance(self.params, (list, tuple))
                else dict(self.params)
            )
        return msg


@dataclass(frozen=True)
class Reply:
    """A scope -> client reply, correlated to a request by ``id``."""

    id: int
    method: str
    code: int
    result: Any = None
    timestamp: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.code == 0


@dataclass(frozen=True)
class Event:
    """A pushed scope -> client event (no ``id``)."""

    name: str
    timestamp: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)


def encode_message(message: Mapping[str, Any]) -> bytes:
    """Encode one envelope as JSON + ``\\r\\n``, ready for the socket."""
    return json.dumps(message).encode("utf-8") + LINE_TERMINATOR


class MessageDecoder:
    """Incremental decoder for the ``\\r\\n``-delimited command channel.

    Feed raw socket bytes with :meth:`feed`; complete JSON objects are
    returned as dicts. Partial lines stay buffered until the terminator
    arrives, so messages split across TCP segments are reassembled.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        """Consume raw bytes, return every complete message in order."""
        self._buffer.extend(data)
        messages: list[dict[str, Any]] = []
        while True:
            index = self._buffer.find(LINE_TERMINATOR)
            if index < 0:
                break
            line = bytes(self._buffer[:index])
            del self._buffer[: index + len(LINE_TERMINATOR)]
            if not line.strip():
                continue
            messages.append(json.loads(line.decode("utf-8")))
        return messages

    @property
    def pending_bytes(self) -> int:
        """Bytes buffered for a not-yet-terminated line."""
        return len(self._buffer)


def classify(message: Mapping[str, Any]) -> Request | Reply | Event:
    """Classify a decoded envelope as request, reply, or pushed event.

    Replies carry ``jsonrpc`` (and ``code``); events carry ``Event``;
    anything else with ``method`` is a request.
    """
    if "Event" in message:
        return Event(
            name=str(message["Event"]),
            timestamp=message.get("Timestamp"),
            payload=message,
        )
    if "jsonrpc" in message or "code" in message:
        return Reply(
            id=int(message["id"]),
            method=str(message.get("method", "")),
            code=int(message.get("code", -1)),
            result=message.get("result"),
            timestamp=message.get("Timestamp"),
            raw=message,
        )
    return Request(
        id=int(message.get("id", 0)),
        method=str(message["method"]),
        params=message.get("params") or {},
    )
