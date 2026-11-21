"""Widget-layer tests for offline-analysis issue 03 — the recent-projects
list in the app shell: persistence next to the settings file, recording on
every project open/create (with the offline flag), the app menu's "Recent
projects" submenu (one-click reopen via ``win.open-recent``, clear via
``win.clear-recent``), survival across an app "restart", and the polished
offline banner text (states that no telescope is connected). These SKIP in
the headless sandbox (no ``gi`` module, no GTK system libs, no display; see
docs/wiki/assumptions.md) and run on the desktop, where the system packages
``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` are installed (or under
``xvfb-run -a uv run pytest``).

Guards, in order: ``pytest.importorskip("gi")`` (no PyGObject at all),
``gi.require_version`` for the Gtk 4.0 / Adw 1 typelibs, and a
``Gtk.init_check()`` fixture (no display -> skip) — the same pattern as
tests/test_offline_mode_widgets.py / tests/test_ui_widgets.py.

What is asserted when they run: the window loads its recent list from the
settings file's sibling (``tmp_path/recent.json`` in tests); creating a
project through the shared project controller records it (most-recent-first,
``offline=False``) and the menu submenu gains a labeled item whose detailed
action carries the path; opening a project offline records it with
``offline=True``, reveals the banner whose title states no telescope is
connected, and the menu item is labeled "(offline)"; ``win.open-recent``
reopens an offline entry through the store-only offline path (mode stays
offline) and a normal entry through the project controller (mode returns to
connected); ``win.clear-recent`` empties the list + file + menu; a fresh
app/window pair over the same settings file ("relaunch") sees the recorded
entries; and an unknown path in ``win.open-recent`` is a no-op toast.
"""

import shutil

import pytest

gi = pytest.importorskip("gi")

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError as exc:  # typelibs missing
    pytest.skip(f"GTK 4.0 / libadwaita typelibs missing: {exc}", allow_module_level=True)

from gi.repository import Gio, GLib, Gtk

from seestar_controller.store import create_project
from seestar_controller.ui.mode import OFFLINE_BANNER_TITLE, AppMode
from seestar_controller.ui.recent import RECENT_FILE_NAME, RecentEntry, load_recent


@pytest.fixture()
def gtk():
    """GTK initialized, or skip when no display is available (headless)."""
    try:
        ok = Gtk.init_check()
    except Exception as exc:  # pragma: no cover - backend errors  # noqa: BLE001
        pytest.skip(f"Gtk.init_check failed: {exc}")
    if not ok:
        pytest.skip("no display available (headless environment)")
    return Gtk


@pytest.fixture()
def app(gtk, tmp_path):
    """A SeestarApplication with its settings file in tmp_path (pattern:
    tests/test_ui_widgets.py)."""
    from seestar_controller.ui.app import SeestarApplication

    return SeestarApplication(settings_path=str(tmp_path / "settings.json"))


@pytest.fixture()
def project_root(app, tmp_path):
    """Point the app's project root at a tmp dir and refresh the list."""
    root = tmp_path / "projects"
    root.mkdir()
    app.state.set_setting("project_root", str(root))
    app.state.save()
    app.project_controller.refresh()
    return root


@pytest.fixture()
def window(app, gtk):
    from seestar_controller.ui.app import SeestarWindow

    return SeestarWindow(app, app.state)


# --- menu-model helpers ---------------------------------------------------------------


def _item_label(menu: Gio.Menu, index: int) -> str | None:
    variant = menu.get_item_attribute_value(index, "label", GLib.VariantType.new("s"))
    return variant.get_string() if variant is not None else None


def _item_action(menu: Gio.Menu, index: int) -> str | None:
    variant = menu.get_item_attribute_value(index, "action", GLib.VariantType.new("s"))
    return variant.get_string() if variant is not None else None


def _item_target(menu: Gio.Menu, index: int) -> str | None:
    """The action target of a menu item (the path for ``win.open-recent``)."""
    variant = menu.get_item_attribute_value(index, "target", None)
    if variant is None:
        return None
    if variant.get_type_string() == "s":
        return variant.get_string()
    return variant.print_(True)  # maybe-type or other: the printed form


def _app_menu(window) -> Gio.Menu:
    return window.settings_button.get_menu_model()


def _recent_submenu(window) -> Gio.Menu | None:
    """The "Recent projects" submenu model of the app menu (or None)."""
    menu = _app_menu(window)
    for i in range(menu.get_n_items()):
        if _item_label(menu, i) == "Recent projects":
            return menu.get_item_link(i, "submenu")
    return None


def _flatten(menu: Gio.Menu):
    """Yield ``(label, action, target)`` for every item, recursing into
    section links (a section item itself has no label)."""
    for i in range(menu.get_n_items()):
        section = menu.get_item_link(i, "section")
        if section is not None:
            yield from _flatten(section)
            continue
        yield _item_label(menu, i), _item_action(menu, i), _item_target(menu, i)


def _recent_items(window) -> list[tuple[str | None, str | None, str | None]]:
    submenu = _recent_submenu(window)
    assert submenu is not None, "the app menu has no 'Recent projects' submenu"
    return list(_flatten(submenu))


def _recent_labels(window) -> list[str | None]:
    return [item[0] for item in _recent_items(window)]


def _recent_actions(window) -> list[str | None]:
    return [item[1] for item in _recent_items(window)]


def _recent_targets(window) -> list[str | None]:
    return [item[2] for item in _recent_items(window)]


# --- the list lives next to the settings file -------------------------------------------


def test_window_loads_recent_list_next_to_settings_file(app, window, tmp_path):
    assert window.recent.path == tmp_path / RECENT_FILE_NAME
    assert window.recent.entries == ()
    # the menu exists and shows the empty placeholder
    labels = _recent_labels(window)
    assert labels == ["No recent projects"]
    assert _recent_actions(window) == [None]
    assert _recent_targets(window) == [None]
    # the actions are registered on the window
    assert window.lookup_action("open-recent") is not None
    assert window.lookup_action("clear-recent") is not None


# --- recording on create / open -----------------------------------------------------------


def test_create_project_records_recent_and_menu_shows_it(app, window, project_root):
    project = app.project_controller.create_project("M 42")
    entry = RecentEntry(path=str(project.path), name="M 42", offline=False)
    assert window.recent.entries == (entry,)
    # persisted next to the settings file
    assert load_recent(window.recent.path) == (entry,)
    # the menu submenu gained the labeled item with the path as its target
    assert _recent_labels(window)[0] == "M 42"
    assert _recent_actions(window)[0] == "win.open-recent"
    assert _recent_targets(window)[0] == str(project.path)
    # plus the clear item in its own section
    assert "Clear recent projects" in _recent_labels(window)


def test_open_project_offline_records_offline_entry_banner_and_menu(
    app, window, project_root
):
    project = create_project("M 31", root=project_root)
    app.project_controller.refresh()
    window.open_project_offline(str(project.path))
    entry = window.recent.entry_for(project.path)
    assert entry is not None
    assert entry.offline is True
    assert entry.name == "M 31"
    # persisted with the offline flag
    assert load_recent(window.recent.path) == (entry,)
    # the menu item is labeled with the offline marker
    assert _recent_labels(window)[0] == "M 31 (offline)"
    # the banner is revealed and states that no telescope is connected
    assert window.mode == AppMode.OFFLINE
    assert window.offline_banner.get_revealed() is True
    title = window.offline_banner.get_title()
    assert title == OFFLINE_BANNER_TITLE
    assert "no telescope connected" in title


def test_normal_open_after_offline_records_connected_entry(app, window, project_root):
    offline_project = create_project("M 31", root=project_root)
    normal_project = create_project("M 42", root=project_root)
    app.project_controller.refresh()
    window.open_project_offline(str(offline_project.path))
    # a normal open switches back to connected and records offline=False
    app.project_controller.open_project(normal_project.path)
    assert window.mode == AppMode.CONNECTED
    assert window.offline_banner.get_revealed() is False
    entries = window.recent.entries
    assert [e.path for e in entries] == [str(normal_project.path), str(offline_project.path)]
    assert entries[0].offline is False
    assert entries[1].offline is True
    assert _recent_labels(window)[0] == "M 42"
    assert _recent_labels(window)[1] == "M 31 (offline)"


# --- one-click reopen via win.open-recent ---------------------------------------------------


def test_open_recent_action_reopens_offline_entry_offline(app, window, project_root):
    project = create_project("M 42", root=project_root)
    app.project_controller.refresh()
    window.open_project_offline(str(project.path))
    assert window.mode == AppMode.OFFLINE
    # one click on the menu item reopens it through the offline path
    window.activate_action("win.open-recent", GLib.Variant("s", str(project.path)))
    assert app.project_controller.current is not None
    assert str(app.project_controller.current.path) == str(project.path)
    assert window.mode == AppMode.OFFLINE  # reopened through the offline path
    assert window.offline_banner.get_revealed() is True


def test_open_recent_action_reopens_normal_entry_connected(app, window, project_root):
    project = app.project_controller.create_project("M 42")
    assert window.mode == AppMode.CONNECTED
    # go offline first, then reopen the normal entry -> back to connected
    other = create_project("M 31", root=project_root)
    app.project_controller.refresh()
    window.open_project_offline(str(other.path))
    assert window.mode == AppMode.OFFLINE
    window.activate_action("win.open-recent", GLib.Variant("s", str(project.path)))
    assert str(app.project_controller.current.path) == str(project.path)
    assert window.mode == AppMode.CONNECTED
    assert window.offline_banner.get_revealed() is False


def test_open_recent_unknown_path_is_a_noop(app, window, project_root):
    app.project_controller.create_project("M 42")
    current_before = app.project_controller.current
    window.activate_action("win.open-recent", GLib.Variant("s", "/no/such/project"))
    assert app.project_controller.current is current_before
    assert window.recent.entries[0].path != "/no/such/project"


def test_open_recent_missing_project_toasts_and_keeps_state(app, window, project_root):
    project = app.project_controller.create_project("M 42")
    # delete the project behind the controller's back -> reopen fails cleanly
    shutil.rmtree(project.path)
    window.activate_action("win.open-recent", GLib.Variant("s", str(project.path)))
    assert window.mode == AppMode.CONNECTED  # unchanged, no crash


# --- clear + restart persistence -------------------------------------------------------------


def test_clear_recent_action_empties_list_file_and_menu(app, window, project_root):
    app.project_controller.create_project("M 42")
    create_project("M 31", root=project_root)
    app.project_controller.refresh()
    window.open_project_offline(str(project_root / "m-31"))
    assert len(window.recent.entries) == 2
    window.activate_action("win.clear-recent", None)
    assert window.recent.entries == ()
    assert load_recent(window.recent.path) == ()
    assert _recent_labels(window) == ["No recent projects"]


def test_relaunch_sees_recorded_entries(app, window, project_root, tmp_path):
    """Open offline, "quit", relaunch a fresh app+window over the same
    settings file: the recent list is loaded from disk and the menu offers
    the offline entry for one-click reopen."""
    project = create_project("M 42", root=project_root)
    app.project_controller.refresh()
    window.open_project_offline(str(project.path))
    settings_path = str(tmp_path / "settings.json")

    from seestar_controller.ui.app import SeestarApplication, SeestarWindow

    app2 = SeestarApplication(settings_path=settings_path)
    window2 = SeestarWindow(app2, app2.state)
    assert window2.recent.path == tmp_path / RECENT_FILE_NAME
    entries = window2.recent.entries
    assert len(entries) == 1
    assert entries[0].path == str(project.path)
    assert entries[0].offline is True
    assert _recent_labels(window2)[0] == "M 42 (offline)"
    # one click reopens it offline
    window2.activate_action("win.open-recent", GLib.Variant("s", str(project.path)))
    assert window2.mode == AppMode.OFFLINE
    assert str(app2.project_controller.current.path) == str(project.path)
