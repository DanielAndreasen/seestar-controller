"""The Link: control connection to the Scope.

- :mod:`.protocol` — wire envelope on the command channel (TCP :4700)
- :mod:`.auth` — firmware >= 7.18 PEM-signed handshake
- :mod:`.keys` — PEM key management (config path, load/save 0600, extraction)
- :mod:`.transport` — client transport (connect, RPC correlation, events, heartbeat)
- :mod:`.stacking` — stacking wire contract (methods, Stack event, param ranges)
- :mod:`.device` — device facade (connect, state, goto, stack, park, event callbacks)
- :mod:`.image` — binary image frames on :4800 (tele) / :4804 (wide)
- :mod:`.mock_scope` — mock Scope test double speaking the same protocol

:mod:`.device` and :mod:`.mock_scope` are imported lazily (PEP 562
``__getattr__``) so those modules can also run standalone via
``python -m seestar_controller.link.device`` / ``...mock_scope``.
"""

from . import auth, image, keys, protocol, stacking
from .keys import (
    InvalidKeyError,
    KeyExtractionError,
    KeyManagementError,
    MissingKeyError,
)
from .transport import ConnectionLostError, LinkTransport, RpcError

#: Device-facade names re-exported lazily from :mod:`.device`.
_DEVICE_EXPORTS = ("Device", "DeviceState", "StackingProgress", "GotoTimeoutError")


def __getattr__(name: str):
    if name in _DEVICE_EXPORTS:
        from . import device

        return getattr(device, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "auth",
    "device",
    "image",
    "keys",
    "protocol",
    "stacking",
    "Device",
    "DeviceState",
    "StackingProgress",
    "GotoTimeoutError",
    "LinkTransport",
    "RpcError",
    "ConnectionLostError",
    "KeyManagementError",
    "MissingKeyError",
    "InvalidKeyError",
    "KeyExtractionError",
]
