"""The Link: control connection to the Scope.

- :mod:`.protocol` — wire envelope on the command channel (TCP :4700)
- :mod:`.auth` — firmware >= 7.18 PEM-signed handshake
- :mod:`.keys` — PEM key management (config path, load/save 0600, extraction)
- :mod:`.transport` — client transport (connect, RPC correlation, events, heartbeat, reconnect)
- :mod:`.resilience` — connection states, reconnect policy, lifecycle event names
- :mod:`.protocol_log` — rotating on-disk log of all JSON-RPC traffic (key material redacted)
- :mod:`.stacking` — stacking wire contract (methods, Stack event, param ranges)
- :mod:`.frames` — frame-download wire contract (list_frames/get_frame), atomic
  .fits + .json sidecar writes, :func:`~.frames.download_frames`
- :mod:`.device` — device facade (connect, state, goto, stack, park, downloads, events)
- :mod:`.image` — binary image frames on :4800 (tele) / :4804 (wide)
- :mod:`.preview` — Preview stream: decoded live frames from :4800/:4804
- :mod:`.mock_scope` — mock Scope test double speaking the same protocol

:mod:`.device` and :mod:`.preview` are imported lazily (PEP 562
``__getattr__``) so those modules can also run standalone via
``python -m seestar_controller.link.device`` / ``...preview``.
"""

from . import auth, frames, image, keys, protocol, protocol_log, resilience, stacking
from .keys import (
    InvalidKeyError,
    KeyExtractionError,
    KeyManagementError,
    MissingKeyError,
)
from .protocol_log import ProtocolLog
from .resilience import (
    CONNECTION_LOST_EVENT,
    RECONNECTED_EVENT,
    RECONNECTING_EVENT,
    SCOPE_ERROR_EVENT,
    STACK_FINISHED_EVENT,
    ConnectionState,
    ReconnectPolicy,
)
from .transport import ConnectionLostError, LinkTransport, RpcError

#: Device-facade names re-exported lazily from :mod:`.device`.
_DEVICE_EXPORTS = ("Device", "DeviceState", "StackingProgress", "GotoTimeoutError")
#: Preview-stream names re-exported lazily from :mod:`.preview`.
_PREVIEW_EXPORTS = (
    "PreviewStream",
    "PreviewFrame",
    "PreviewCallback",
    "FrameThrottle",
    "decode_payload",
)


def __getattr__(name: str):
    if name in _DEVICE_EXPORTS:
        from . import device

        return getattr(device, name)
    if name in _PREVIEW_EXPORTS:
        from . import preview

        return getattr(preview, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "auth",
    "device",
    "frames",
    "image",
    "keys",
    "preview",
    "protocol",
    "protocol_log",
    "resilience",
    "stacking",
    "Device",
    "DeviceState",
    "StackingProgress",
    "GotoTimeoutError",
    "PreviewStream",
    "PreviewFrame",
    "PreviewCallback",
    "FrameThrottle",
    "decode_payload",
    "LinkTransport",
    "RpcError",
    "ConnectionLostError",
    "ProtocolLog",
    "ConnectionState",
    "ReconnectPolicy",
    "CONNECTION_LOST_EVENT",
    "RECONNECTING_EVENT",
    "RECONNECTED_EVENT",
    "SCOPE_ERROR_EVENT",
    "STACK_FINISHED_EVENT",
    "KeyManagementError",
    "MissingKeyError",
    "InvalidKeyError",
    "KeyExtractionError",
]
