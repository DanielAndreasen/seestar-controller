import pytest

from seestar_controller.link import auth
from seestar_controller.link.mock_scope import MockScope
from seestar_controller.link.transport import LinkTransport


@pytest.fixture(scope="session")
def keypair():
    """A generated RSA test keypair: (private_pem, public_pem).

    Stands in for the interop PEM extracted from the official app — the
    real key is unavailable (user AFK, see docs/wiki/assumptions.md).
    """
    return auth.generate_keypair()


@pytest.fixture()
def mock_scope(keypair):
    """A running mock Scope on ephemeral ports, verifying signed handshakes."""
    _private_pem, public_pem = keypair
    scope = MockScope(
        "127.0.0.1", 0, 0, 0, public_pem=public_pem, frame_interval=0.01
    )
    scope.start()
    yield scope
    scope.stop()


@pytest.fixture()
def transport(mock_scope, keypair):
    """A LinkTransport connected to the mock Scope with a signed handshake."""
    private_pem, _public_pem = keypair
    t = LinkTransport(
        "127.0.0.1",
        mock_scope.command_port,
        pem=private_pem,
        heartbeat_interval=0.1,
        heartbeat_timeout=0.5,
        request_timeout=2.0,
    )
    t.connect()
    yield t
    t.disconnect()
