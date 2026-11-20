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

Observing ticket 03 adds the Observe-view wiring tests: the two-pane
:class:`~seestar_controller.observing.observe_view.ObserveView` (queue editor
+ night controls on the left, preview picture + progress rows on the right)
built via ``build_view("observe", queue_controller=..., observe_controller=...)``
against a stub observe controller — the Start/Pause/Skip/Stop/Park buttons
call the controller's passthroughs (with an inline error when start fails),
:class:`~seestar_controller.observing.ObserveViewState` snapshots re-render
the progress rows / preview picture / button sensitivity, and the app owns
one shared :class:`~seestar_controller.observing.ObserveController` that the
window passes down to the Observe page.
"""

import dataclasses
from types import SimpleNamespace

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
from seestar_controller.link.image import Frame  # noqa: E402
from seestar_controller.link.preview import PreviewFrame, decode_payload  # noqa: E402
from seestar_controller.observing import (  # noqa: E402
    EVENT_ADDED,
    ExecutorState,
    ObserveStateError,
    ObserveViewState,
    PlanQueueController,
    QueueEntry,
)
from seestar_controller.ui.connection import ConnectionStatus  # noqa: E402

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


# --- observing ticket 03: the Observe view wiring -----------------------------------------


def preview_frame(frame_id: int = 1, width: int = 8, height: int = 6) -> PreviewFrame:
    """A small decoded preview frame (grayscale raw wire frame)."""
    size = width * height
    payload = bytes([frame_id % 256]) * size
    frame = Frame(code=0, id=frame_id, width=width, height=height, size=size, payload=payload)
    return PreviewFrame(
        source="tele", image=decode_payload(frame), frame=frame, timestamp=0.0
    )


class StubObserveController:
    """Duck-typed :class:`~seestar_controller.observing.ObserveController` for
    widget tests — records the calls, serves crafted snapshots."""

    def __init__(
        self,
        queue_controller,
        *,
        status=ConnectionStatus.CONNECTED,
        project=None,
    ):
        self._queue_controller = queue_controller
        self.connection = SimpleNamespace(status=status)
        self.project = project
        self._snapshot = ObserveViewState(
            executor_state=ExecutorState.IDLE,
            queue_index=None,
            entry=None,
            frames_stacked=0,
            elapsed_s=0.0,
            error=None,
            session=None,
            queue_entries=queue_controller.entries,
            preview_frame=None,
            preview_running=False,
            night_active=False,
        )
        self.listeners: list = []
        self.calls: list = []
        self.start_error: Exception | None = None

    @property
    def queue_controller(self):
        return self._queue_controller

    @property
    def snapshot(self):
        return self._snapshot

    def set_snapshot(self, **changes):
        """Replace the snapshot and notify the view listeners (like the real
        controller's emission)."""
        self._snapshot = dataclasses.replace(self._snapshot, **changes)
        for listener in list(self.listeners):
            listener(self._snapshot)

    def add_listener(self, listener):
        self.listeners.append(listener)
        return listener

    def remove_listener(self, listener):
        if listener in self.listeners:
            self.listeners.remove(listener)

    def start_night(self, project=None, **kwargs):
        self.calls.append(("start_night", project, kwargs))
        if self.start_error is not None:
            raise self.start_error
        return SimpleNamespace()

    def pause(self):
        self.calls.append(("pause",))

    def resume(self):
        self.calls.append(("resume",))

    def skip(self):
        self.calls.append(("skip",))

    def stop(self):
        self.calls.append(("stop",))

    def park(self):
        self.calls.append(("park",))


@pytest.fixture()
def observe_stub(controller):
    return StubObserveController(controller, project=object())


def test_observe_view_two_pane_layout(gtk, controller, observe_stub):
    from seestar_controller.ui.views import build_view

    view = build_view(
        "observe", queue_controller=controller, observe_controller=observe_stub
    )
    assert view.view_id == "observe"
    assert view.observe_controller is observe_stub
    widget = view.observe_widget
    assert widget.controller is observe_stub
    assert widget.queue_controller is controller
    assert widget.queue_editor.controller is controller
    # two panes: editor + controls | preview + progress
    assert widget.paned.get_start_child() is not None
    assert widget.paned.get_end_child() is not None
    assert widget.picture is not None
    for name in ("start_button", "pause_button", "skip_button", "stop_button", "park_button"):
        assert getattr(widget, name) is not None
    assert set(widget.row_subtitles()) == {
        "target",
        "state",
        "frames",
        "elapsed",
        "queue",
        "session",
    }
    # the idle snapshot is rendered
    subs = widget.row_subtitles()
    assert subs["target"] == "—"
    assert subs["state"] == "idle"
    assert subs["frames"] == "0"
    assert subs["elapsed"] == "0 s"
    assert subs["queue"] == "0 / 0"
    assert subs["session"] == "—"
    # start needs a project, park needs a connection
    assert not widget.start_button.get_sensitive()
    assert widget.park_button.get_sensitive()
    assert not widget.pause_button.get_sensitive()


def test_night_control_buttons_call_the_controller(gtk, controller, observe_stub):
    from seestar_controller.ui.views import build_view

    view = build_view(
        "observe", queue_controller=controller, observe_controller=observe_stub
    )
    widget = view.observe_widget
    widget.start_button.emit("clicked")
    widget.pause_button.emit("clicked")  # idle -> pause passthrough
    widget.skip_button.emit("clicked")
    widget.stop_button.emit("clicked")
    widget.park_button.emit("clicked")
    assert [c[0] for c in observe_stub.calls] == [
        "start_night",
        "pause",
        "skip",
        "stop",
        "park",
    ]
    assert observe_stub.calls[0][1] is observe_stub.project


def test_pause_button_resumes_when_paused(gtk, controller, observe_stub):
    from seestar_controller.ui.views import build_view

    view = build_view(
        "observe", queue_controller=controller, observe_controller=observe_stub
    )
    widget = view.observe_widget
    observe_stub.set_snapshot(
        executor_state=ExecutorState.PAUSED, night_active=True
    )
    assert widget.pause_button.get_label() == "Resume"
    widget.pause_button.emit("clicked")
    assert observe_stub.calls[-1] == ("resume",)


def test_start_button_shows_inline_error_when_start_fails(gtk, controller, observe_stub):
    from seestar_controller.ui.views import build_view

    observe_stub.start_error = ObserveStateError(
        "not connected to the scope — connect first (header bar)"
    )
    view = build_view(
        "observe", queue_controller=controller, observe_controller=observe_stub
    )
    widget = view.observe_widget
    widget.start_button.emit("clicked")
    label = widget.status_label.get_label()
    assert "not connected" in label
    assert widget.status_label.has_css_class("error")


def test_snapshot_updates_progress_labels_preview_and_buttons(gtk, controller, observe_stub):
    from seestar_controller.ui.views import build_view

    view = build_view(
        "observe", queue_controller=controller, observe_controller=observe_stub
    )
    widget = view.observe_widget
    entry = QueueEntry.resolved("M 42", COORD_A)
    observe_stub.set_snapshot(
        executor_state=ExecutorState.STACKING,
        queue_index=0,
        entry=entry,
        frames_stacked=7,
        elapsed_s=65.0,
        session="sess-1",
        queue_entries=(entry,),
        preview_frame=preview_frame(5),
        preview_running=True,
        night_active=True,
    )
    subs = widget.row_subtitles()
    assert subs["target"] == "M 42 — RA 5.5881h Dec -5.3911°"
    assert subs["state"] == "stacking"
    assert subs["frames"] == "7"
    assert subs["elapsed"] == "65 s"
    assert subs["queue"] == "1 / 1"
    assert subs["session"] == "sess-1"
    # the preview picture got a texture from the frame
    assert widget.picture.get_paintable() is not None
    assert "tele frame #5" in widget.preview_label.get_label()
    # night active: start disabled, pause/skip/stop enabled
    assert not widget.start_button.get_sensitive()
    assert widget.pause_button.get_sensitive()
    assert widget.skip_button.get_sensitive()
    assert widget.stop_button.get_sensitive()
    assert widget.pause_button.get_label() == "Pause"

    # DONE: start enabled again (connected + project), status line says done
    observe_stub.set_snapshot(
        executor_state=ExecutorState.DONE, night_active=False, preview_running=False
    )
    assert widget.start_button.get_sensitive()
    assert "done" in widget.status_label.get_label()

    # an executor error shows inline
    observe_stub.set_snapshot(
        executor_state=ExecutorState.ABORTED, error="slew failed", night_active=False
    )
    assert "slew failed" in widget.status_label.get_label()
    assert widget.status_label.has_css_class("error")


def test_app_owns_shared_observe_controller_and_window_passes_it_down(app, gtk):
    from seestar_controller.observing import ObserveController
    from seestar_controller.ui.app import SeestarWindow
    from seestar_controller.ui.views import ObserveView

    assert isinstance(app.observe_controller, ObserveController)
    assert app.observe_controller.connection is app.connection_controller
    assert app.observe_controller.queue_controller is app.queue_controller
    window = SeestarWindow(app, app.state)
    assert window.observe_controller is app.observe_controller
    observe_page = window.view_stack.get_child_by_name("observe")
    assert isinstance(observe_page, ObserveView)
    assert observe_page.observe_controller is app.observe_controller
    assert observe_page.observe_widget.controller is app.observe_controller
    assert observe_page.queue_editor.controller is app.queue_controller
