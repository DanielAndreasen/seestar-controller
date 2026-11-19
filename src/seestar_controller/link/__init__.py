"""The Link: control connection to the Scope.

- :mod:`.protocol` — wire envelope on the command channel (TCP :4700)
- :mod:`.auth` — firmware >= 7.18 PEM-signed handshake
- :mod:`.transport` — client transport (connect, RPC correlation, events, heartbeat)
- :mod:`.image` — binary image frames on :4800 (tele) / :4804 (wide)
- :mod:`.mock_scope` — mock Scope test double speaking the same protocol
  (imported lazily as ``seestar_controller.link.mock_scope`` so the module
  can also run standalone via ``python -m``)
"""

from . import auth, image, protocol
from .transport import ConnectionLostError, LinkTransport, RpcError

__all__ = [
    "auth",
    "image",
    "protocol",
    "LinkTransport",
    "RpcError",
    "ConnectionLostError",
]
