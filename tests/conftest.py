import os
import sqlite3
from types import SimpleNamespace

import pytest

from seestar_controller.link import auth
from seestar_controller.link.mock_scope import MockScope
from seestar_controller.link.transport import LinkTransport
from seestar_controller.ui.connection import ConnectionController
from seestar_controller.ui.notifications import NotificationBus, NotificationController
from seestar_controller.ui.settings import SettingsController
from seestar_controller.ui.state import AppState

# Keep the test suite hermetic: the protocol log is ON by default (it writes
# under ~/.config/seestar-controller), so disable it for the whole session
# unless a test opts in explicitly (protocol_log=... / monkeypatch.setenv).
os.environ["SEESTAR_PROTOCOL_LOG"] = "0"

# --- test-only performance shims (dependency-upgrade ticket 05) -------------------
#
# The suite builds synthetic projects with hundreds of FITS frames + sqlite
# rows; every store_frame()/add_measurement() fsyncs the file and the WAL
# commit (~30 ms per point on this machine, ~22 s for one 710-point synthetic
# lightcurve). Durability against power loss is not observable in-process, so
# for the whole test session: (a) os.fsync becomes a no-op — the atomic-write
# failure-path tests monkeypatch os.fsync themselves and are unaffected — and
# (b) every sqlite connection opened by the store runs with
# PRAGMA synchronous=OFF (commits stay visible to all in-process readers;
# journal_mode stays WAL, which test_store_project asserts). No assertion in
# the suite depends on a real fsync.
_real_fsync = os.fsync
_real_connect = sqlite3.connect


def _noop_fsync(fd: int) -> None:
    return None


def _fast_connect(*args, **kwargs):
    conn = _real_connect(*args, **kwargs)
    try:
        conn.execute("PRAGMA synchronous=OFF")
    except sqlite3.Error:  # pragma: no cover - defensive
        pass
    return conn


os.fsync = _noop_fsync  # ty: ignore[invalid-assignment]
sqlite3.connect = _fast_connect  # ty: ignore[invalid-assignment]


@pytest.fixture(scope="session", autouse=True)
def _restore_io_shims():
    """Restore the real fsync / sqlite3.connect when the session ends."""
    yield
    os.fsync = _real_fsync
    sqlite3.connect = _real_connect


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


# --- ui-shell headless harness (ticket 05) ----------------------------------------
#
# The ui package's pure layer (state / settings / connection / notifications)
# is fully testable without a display or GTK (docs/wiki/assumptions.md — the
# sandbox has no gi). The helpers below build the app-level controllers exactly
# like SeestarApplication does, but with a FakeDevice — no network, no gi — so
# view tickets can drive controller logic headless (see tests/test_ui_harness.py).


class FakeDevice:
    """Duck-typed stand-in for the link Device facade — no network, no gi.

    Matches the facade's event-handler shape (``add_event_handler(name,
    handler)`` / ``remove_event_handler(name, handler)``); :meth:`emit`
    dispatches a pushed event to the registered handlers, like the transport's
    dispatch does.
    """

    def __init__(self, scope_state=None):
        self._scope_state = scope_state or SimpleNamespace(mode="idle", target=None)
        self.handlers: dict[str, list] = {}
        self.disconnected = False

    def state(self):
        return self._scope_state

    def add_event_handler(self, name, handler):
        self.handlers.setdefault(name, []).append(handler)

    def remove_event_handler(self, name, handler):
        handlers = self.handlers.get(name, [])
        if handler in handlers:
            handlers.remove(handler)

    def disconnect(self):
        self.disconnected = True

    def emit(self, name, event=None):
        """Dispatch a pushed event named ``name`` to its registered handlers."""
        for handler in list(self.handlers.get(name, ())):
            handler(event)


@pytest.fixture()
def ui_harness(tmp_path):
    """The app-level pure ui controllers with fakes — headless, no network, no gi.

    Mirrors what ``SeestarApplication`` owns: an :class:`AppState` (settings
    file inside ``tmp_path``), the ``SettingsController``, the
    ``ConnectionController`` (fake device factory), the ``NotificationBus``,
    and the ``NotificationController``. Returns them as a namespace together
    with the fake ``device`` and the ``tmp_path``.
    """
    state = AppState(tmp_path / "settings.json")
    state.set_setting("scope_ip", "192.168.1.50")
    state.set_setting("project_root", str(tmp_path))  # an existing directory
    settings = SettingsController(state)
    device = FakeDevice()
    connection = ConnectionController(settings, device_factory=lambda ip, key_path: device)
    bus = NotificationBus()
    notifications = NotificationController(bus, connection)
    return SimpleNamespace(
        tmp_path=tmp_path,
        state=state,
        settings=settings,
        connection=connection,
        bus=bus,
        notifications=notifications,
        device=device,
    )
