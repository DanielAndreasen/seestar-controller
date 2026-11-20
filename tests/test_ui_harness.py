"""Headless test harness for the ui shell (ui-shell ticket 05) — pure tests, no gi.

What lives here:

- the **pure/widget boundary audit**: a subprocess test that imports the ui
  package AND every pure module (``state`` / ``settings`` / ``connection`` /
  ``notifications``) and asserts ``gi`` never lands in ``sys.modules`` (and no
  widget module, no ``link``, is pulled in), plus a source scan asserting every
  module of ``seestar_controller/ui`` is either pure (no ``gi`` import in its
  source) or a widget module (``gi`` at module top, lazy-exported from the
  package via PEP 562 — ``ui._WIDGET_EXPORTS`` covers exactly the widget
  modules);
- **harness tests** proving the ``ui_harness`` conftest fixture (the app-level
  controllers with a :class:`FakeDevice` — no network, no gi) drives the
  connection state machine, the notification bus, and settings persistence
  headless, so future view tickets can test controller logic the same way.

Everything in this file runs with no display (the sandbox has no GTK, see
docs/wiki/assumptions.md); the widget smoke tests live in
``tests/test_ui_widgets.py`` and skip without ``gi``.
"""

import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from seestar_controller import ui

#: Directory of the ui package (source-scan audit).
UI_PACKAGE_DIR = Path(ui.__file__).resolve().parent

#: The pure ui modules — no ``gi`` import allowed (subprocess-enforced).
PURE_UI_MODULES = (
    "__init__",
    "state",
    "settings",
    "connection",
    "notifications",
    "project_controller",
)

#: The widget ui modules — ``gi`` at module top, lazy-exported via PEP 562.
WIDGET_UI_MODULES = (
    "app",
    "views",
    "settings_dialog",
    "connection_panel",
    "toast",
    "notification_log",
)


# --- pure/widget boundary audit ------------------------------------------------------


def test_every_pure_ui_module_imports_no_gi_in_a_subprocess():
    """Import the package + EVERY pure module in a subprocess; assert no
    ``gi`` (and no widget module, no ``link``) ends up in ``sys.modules``."""
    imports = "".join(
        f"import seestar_controller.ui.{name};" for name in PURE_UI_MODULES[1:]
    )
    widget_asserts = "".join(
        f"assert 'seestar_controller.ui.{name}' not in sys.modules, "
        f"'{name} imported eagerly';"
        for name in WIDGET_UI_MODULES
    )
    code = (
        "import sys;"
        "import seestar_controller.ui;"
        f"{imports}"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        f"{widget_asserts}"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_every_ui_module_is_pure_or_widget_and_widgets_are_lazy_exported():
    """Source-scan audit: every ``ui/*.py`` is either pure (no ``gi`` import)
    or a widget module (``gi`` at module top), and the package's lazy exports
    cover exactly the widget modules."""
    modules = {path.stem for path in UI_PACKAGE_DIR.glob("*.py")}
    assert modules == set(PURE_UI_MODULES) | set(WIDGET_UI_MODULES)
    for name in PURE_UI_MODULES:
        source = (UI_PACKAGE_DIR / f"{name}.py").read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), (
            f"pure module {name} imports gi"
        )
    for name in WIDGET_UI_MODULES:
        lines = (UI_PACKAGE_DIR / f"{name}.py").read_text(encoding="utf-8").splitlines()
        gi_lines = [i for i, line in enumerate(lines) if line.strip() == "import gi"]
        assert gi_lines, f"widget module {name} has no 'import gi' at module top"
        repo_line = next(
            i for i, line in enumerate(lines) if line.startswith("from gi.repository")
        )
        assert gi_lines[0] < repo_line, (
            f"widget module {name}: 'import gi' must come before 'from gi.repository'"
        )
    # every widget module is reachable only lazily through the package exports
    assert set(ui._WIDGET_EXPORTS.values()) == set(WIDGET_UI_MODULES)


# --- the headless harness (conftest ui_harness fixture) --------------------------------


def test_harness_builds_the_app_level_controllers_headless(ui_harness):
    from seestar_controller.ui.connection import ConnectionController, ConnectionStatus
    from seestar_controller.ui.notifications import (
        NotificationBus,
        NotificationController,
    )
    from seestar_controller.ui.settings import SettingsController
    from seestar_controller.ui.state import AppState

    assert isinstance(ui_harness.state, AppState)
    assert isinstance(ui_harness.settings, SettingsController)
    assert isinstance(ui_harness.connection, ConnectionController)
    assert isinstance(ui_harness.bus, NotificationBus)
    assert isinstance(ui_harness.notifications, NotificationController)
    # wired like the app: one shared settings source, one shared bus
    assert ui_harness.settings.state is ui_harness.state
    assert ui_harness.connection.settings is ui_harness.settings
    assert ui_harness.notifications.bus is ui_harness.bus
    assert ui_harness.notifications.connection is ui_harness.connection
    # defaults: disconnected, empty bus, settings file not yet written
    assert ui_harness.connection.status == ConnectionStatus.DISCONNECTED
    assert len(ui_harness.bus) == 0
    assert not (ui_harness.tmp_path / "settings.json").exists()
    assert ui_harness.state.get_setting("scope_ip") == "192.168.1.50"


def test_harness_drives_connect_disconnect_with_the_fake_device(ui_harness):
    from seestar_controller.ui.connection import ConnectionStatus

    ui_harness.connection.connect()
    assert ui_harness.connection.wait(5)
    assert ui_harness.connection.status == ConnectionStatus.CONNECTED
    assert ui_harness.connection.device is ui_harness.device
    snapshot = ui_harness.connection.snapshot
    assert snapshot.scope_ip == "192.168.1.50"
    assert snapshot.scope_state is ui_harness.device.state()

    ui_harness.connection.disconnect()
    assert ui_harness.connection.status == ConnectionStatus.DISCONNECTED
    assert ui_harness.device.disconnected


def test_harness_notification_controller_translates_device_events(ui_harness):
    from seestar_controller.ui.notifications import NotificationSeverity

    ui_harness.connection.connect()
    assert ui_harness.connection.wait(5)
    # the notification controller subscribed to the fake device on connect
    assert "Stack" in ui_harness.device.handlers
    assert "scope_error" in ui_harness.device.handlers

    ui_harness.device.emit(
        "Stack",
        SimpleNamespace(payload={"state": "working", "frames": 3, "lapse_ms": 30000}),
    )
    ui_harness.device.emit(
        "scope_error",
        SimpleNamespace(
            payload={"method": "scope_goto_target", "code": -1, "result": {"msg": "busy"}}
        ),
    )
    log = ui_harness.bus.log()  # newest first
    assert len(log) == 2
    assert log[0].severity == NotificationSeverity.ERROR
    assert log[0].title == "Scope error"
    assert "scope_goto_target" in log[0].message
    assert log[1].severity == NotificationSeverity.INFO
    assert log[1].title == "Stacking"
    assert "3 sub-frames" in log[1].message

    # detach unregisters everything (no listener leaks)
    ui_harness.notifications.detach()
    assert ui_harness.connection.listeners == ()
    ui_harness.device.emit(
        "Stack",
        SimpleNamespace(payload={"state": "working", "frames": 4, "lapse_ms": 40000}),
    )
    assert len(ui_harness.bus) == 2


def test_harness_settings_update_persists_atomically_to_tmp(ui_harness):
    from seestar_controller.ui.state import load_state

    seen = []
    ui_harness.settings.add_listener(seen.append)
    new_values = ui_harness.settings.update(scope_ip="10.0.0.2")
    assert new_values["scope_ip"] == "10.0.0.2"
    assert seen == [new_values]
    reloaded = load_state(ui_harness.state.settings_path)
    assert reloaded.get_setting("scope_ip") == "10.0.0.2"
    assert reloaded.get_setting("project_root") == str(ui_harness.tmp_path)
