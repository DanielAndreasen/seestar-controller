"""Widget-layer tests for the observing queue editor — these SKIP in the
headless sandbox (no ``gi`` module, no GTK system libs, no display; see
docs/wiki/assumptions.md) and run on the desktop, where the system packages
``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` are installed (or under
``xvfb-run -a uv run pytest``).

Guards, in order: ``pytest.importorskip("gi")`` (no PyGObject at all),
``gi.require_version`` for the Gtk 4.0 / Adw 1 typelibs, and a
``Gtk.init_check()`` fixture (no display -> skip) — the same pattern as
tests/test_ui_widgets.py.

What is asserted when they run (observing ticket 01): the
:class:`~seestar_controller.observing.queue_editor.QueueEditor` renders one
row per queued entry (``row_count`` / ``row_label`` matching
``QueueEntry.describe()``), the per-row up/down/duplicate/remove buttons drive
the pure controller's ops and the list re-renders on every queue event, the
Add area adds by name (unresolved, or resolved after the Resolve button looked
the name up through an injected fake resolver — the catalog is mocked, never
hit) and by RA/Dec (validated through the real ``catalog.coords`` parsers),
with inline status errors for name+coords together, invalid coordinates, or
an unknown name; and the wiring: ``build_view("observe", queue_controller=...)``
hands the shared controller to the Observe view's editor, the app owns one
shared :class:`~seestar_controller.observing.PlanQueueController`, and the
window passes it down to the Observe page.
"""

import pytest

gi = pytest.importorskip("gi")

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError as exc:  # typelibs missing
    pytest.skip(f"GTK 4.0 / libadwaita typelibs missing: {exc}", allow_module_level=True)

from gi.repository import Gtk  # noqa: E402

from seestar_controller.catalog.coords import J2000Coord  # noqa: E402
from seestar_controller.catalog.simbad import NameNotFoundError  # noqa: E402
from seestar_controller.observing import (  # noqa: E402
    EVENT_ADDED,
    PlanQueueController,
    QueueEntry,
)

COORD_A = J2000Coord(ra_deg=83.8215, dec_deg=-5.39114)  # ~ M 42
COORD_B = J2000Coord(ra_deg=10.68, dec_deg=41.27)  # ~ M 31


def fake_resolver(mapping):
    """A fake catalog resolver: name -> J2000Coord; unknown names raise."""

    def resolver(name):
        if name not in mapping:
            raise NameNotFoundError(f"SIMBAD has no object named {name!r}")
        return mapping[name]

    return resolver


@pytest.fixture()
def gtk():
    """GTK initialized, or skip when no display is available (headless)."""
    try:
        ok = Gtk.init_check()
    except Exception as exc:  # pragma: no cover - backend errors
        pytest.skip(f"Gtk.init_check failed: {exc}")
    if not ok:
        pytest.skip("no display available (headless environment)")
    return Gtk


@pytest.fixture()
def app(gtk, tmp_path):
    """A SeestarApplication with its settings file in tmp_path."""
    from seestar_controller.ui.app import SeestarApplication

    return SeestarApplication(settings_path=str(tmp_path / "settings.json"))


@pytest.fixture()
def controller():
    """A PlanQueueController with a MOCKED catalog resolver (no network)."""
    return PlanQueueController(resolver=fake_resolver({"M 42": COORD_A, "M 31": COORD_B}))


@pytest.fixture()
def editor(gtk, controller):
    from seestar_controller.observing import QueueEditor

    return QueueEditor(controller)


def test_editor_renders_entries(gtk, controller, editor):
    assert editor.controller is controller
    assert editor.row_count() == 0
    controller.add_by_name("M 42", exposure_s=30, gain=80)
    controller.add_by_coords("10.68", "41.27", mosaic=(2.0, 1.0))
    assert editor.row_count() == 2
    assert editor.row_label(0) == controller[0].describe()
    assert editor.row_label(0).startswith("M 42 (unresolved) | no mosaic | 30 s | gain 80")
    assert editor.row_label(1).startswith("RA 0.7120h Dec +41.2700° | mosaic 2°×1° | 10 s | gain 50")
    # the list box itself holds the rows
    assert editor.list_box.get_row_at_index(0) is not None
    assert editor.list_box.get_row_at_index(2) is None


def test_editor_rerenders_on_external_queue_changes(gtk, controller, editor):
    """The editor is a controller listener: changes from anywhere re-render."""
    controller.add_by_name("A")
    assert editor.row_count() == 1
    controller.queue.add(QueueEntry(name="B"))  # direct queue op, still an event
    assert editor.row_count() == 2
    controller.clear()
    assert editor.row_count() == 0


def test_row_buttons_drive_controller_ops(gtk, controller, editor):
    controller.add_by_name("A")
    controller.add_by_name("B")
    controller.add_by_name("C")
    assert editor.row_count() == 3
    seen = []
    controller.add_listener(seen.append)

    editor.click_row_button(1, "up")  # B above A
    assert [e.name for e in controller.entries] == ["B", "A", "C"]
    assert editor.row_label(0).startswith("B")
    assert seen[-1].kind == "moved"

    editor.click_row_button(0, "down")  # B back below A
    assert [e.name for e in controller.entries] == ["A", "B", "C"]

    editor.click_row_button(0, "duplicate")  # A A B C
    assert [e.name for e in controller.entries] == ["A", "A", "B", "C"]
    assert editor.row_count() == 4
    assert seen[-1].kind == "duplicated"

    editor.click_row_button(1, "remove")  # A B C
    assert [e.name for e in controller.entries] == ["A", "B", "C"]
    assert editor.row_count() == 3
    assert seen[-1].kind == "removed"
    assert [e.kind for e in seen] == ["moved", "moved", "duplicated", "removed"]


def test_add_by_name_unresolved_then_resolved(gtk, controller, editor):
    editor.name_entry.set_text("M 42")
    editor.exposure_spin.set_value(30)
    editor.gain_spin.set_value(80)
    editor.add_button.emit("clicked")
    assert editor.row_count() == 1
    entry = controller[0]
    assert entry.name == "M 42" and entry.coord is None  # unresolved until Resolve
    assert entry.exposure_s == 30.0 and entry.gain == 80
    assert "unresolved" in editor.status_label.get_label()
    assert editor.name_entry.get_text() == ""  # fields cleared after a successful add

    # Resolve looks the name up via the (mocked) catalog and shows the coord
    editor.name_entry.set_text("M 42")
    editor.resolve_button.emit("clicked")
    status = editor.status_label.get_label()
    assert "M 42" in status and "RA 5.5881h" in status and "-5.3911" in status
    assert not editor.status_label.has_css_class("error")

    # Add now stores the RESOLVED entry (name + coord)
    editor.add_button.emit("clicked")
    assert editor.row_count() == 2
    resolved = controller[1]
    assert resolved.name == "M 42" and resolved.coord == COORD_A
    assert editor.row_label(1).startswith("M 42 (RA 5.5881h Dec -5.3911°)")


def test_resolve_button_shows_inline_error_for_unknown_name(gtk, controller, editor):
    editor.name_entry.set_text("NO SUCH OBJECT")
    editor.resolve_button.emit("clicked")
    status = editor.status_label.get_label()
    assert "NO SUCH OBJECT" in status and "NameNotFoundError" in status
    assert editor.status_label.has_css_class("error")
    assert editor.row_count() == 0  # nothing added

    # an empty name is an inline error too
    editor.name_entry.set_text("   ")
    editor.resolve_button.emit("clicked")
    assert editor.status_label.has_css_class("error")
    assert editor.row_count() == 0


def test_add_by_coords_valid_and_invalid(gtk, controller, editor):
    editor.ra_entry.set_text("5h35m17.3s")
    editor.dec_entry.set_text("-05°23′28″")
    editor.mosaic_switch.set_active(True)
    editor.pan_spin.set_value(2.0)
    editor.tilt_spin.set_value(1.0)
    editor.add_button.emit("clicked")
    assert editor.row_count() == 1
    entry = controller[0]
    assert entry.name is None and entry.coord is not None
    assert entry.coord.ra_deg == pytest.approx((5 + 35 / 60 + 17.3 / 3600) * 15.0)
    assert entry.coord.dec_deg == pytest.approx(-(5 + 23 / 60 + 28 / 3600))
    assert entry.mosaic == (2.0, 1.0)
    assert "added RA" in editor.status_label.get_label()

    # invalid coordinates: inline error, nothing added
    editor.ra_entry.set_text("abc")
    editor.dec_entry.set_text("0")
    editor.add_button.emit("clicked")
    assert editor.row_count() == 1
    assert "invalid coordinates" in editor.status_label.get_label()
    assert editor.status_label.has_css_class("error")

    # RA without Dec: inline error, nothing added
    editor.ra_entry.set_text("5.5")
    editor.dec_entry.set_text("")
    editor.add_button.emit("clicked")
    assert editor.row_count() == 1
    assert "together" in editor.status_label.get_label()


def test_add_rejects_name_and_coords_together(gtk, controller, editor):
    editor.name_entry.set_text("M 42")
    editor.ra_entry.set_text("5.5")
    editor.dec_entry.set_text("0")
    editor.add_button.emit("clicked")
    assert editor.row_count() == 0
    assert "not both" in editor.status_label.get_label()
    assert editor.status_label.has_css_class("error")


def test_add_with_nothing_entered_is_an_inline_error(gtk, controller, editor):
    editor.add_button.emit("clicked")
    assert editor.row_count() == 0
    assert "name or RA/Dec" in editor.status_label.get_label()
    assert editor.status_label.has_css_class("error")


def test_mosaic_switch_enables_pan_tilt_spins(gtk, editor):
    assert not editor.pan_spin.get_sensitive()
    assert not editor.tilt_spin.get_sensitive()
    editor.mosaic_switch.set_active(True)
    assert editor.pan_spin.get_sensitive() and editor.tilt_spin.get_sensitive()
    editor.mosaic_switch.set_active(False)
    assert not editor.pan_spin.get_sensitive()


def test_build_view_observe_wires_the_shared_controller(gtk, controller):
    from seestar_controller.ui.views import build_view

    view = build_view("observe", queue_controller=controller)
    assert view.view_id == "observe"
    assert view.queue_controller is controller
    assert view.queue_editor.controller is controller
    # the editor edits the shared queue
    view.queue_editor.name_entry.set_text("M 31")
    view.queue_editor.add_button.emit("clicked")
    assert len(controller) == 1 and controller[0].name == "M 31"

    # default construction still works (own controller)
    standalone = build_view("observe")
    assert standalone.queue_controller is not controller
    assert standalone.queue_editor.controller is standalone.queue_controller


def test_app_owns_the_shared_queue_controller_and_window_passes_it_down(app, gtk):
    from seestar_controller.ui.app import SeestarWindow
    from seestar_controller.ui.views import ObserveView

    assert isinstance(app.queue_controller, PlanQueueController)
    window = SeestarWindow(app, app.state)
    assert window.queue_controller is app.queue_controller
    observe_page = window.view_stack.get_child_by_name("observe")
    assert isinstance(observe_page, ObserveView)
    assert observe_page.queue_controller is app.queue_controller
    assert observe_page.queue_editor.controller is app.queue_controller

    # an explicit controller wins over the app's
    other = PlanQueueController()
    window2 = SeestarWindow(app, app.state, queue_controller=other)
    assert window2.queue_controller is other
    assert window2.view_stack.get_child_by_name("observe").queue_editor.controller is other


def test_editor_events_flow_to_controller_listeners(gtk, controller, editor):
    seen = []
    controller.add_listener(seen.append)
    editor.name_entry.set_text("M 42")
    editor.add_button.emit("clicked")
    assert [e.kind for e in seen] == [EVENT_ADDED]
    assert seen[0].entry is controller[0]
