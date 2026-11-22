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
Add area adds by name (only after the Resolve button looked the name up through
an injected fake resolver — the catalog is mocked, never hit; an unresolved or
mismatched name is an inline error that queues nothing) and by RA/Dec
(validated through the real ``catalog.coords`` parsers),
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

Observing ticket 05 adds the stale-session dialog tests: when Start night
raises ``SessionOpenError`` (a stale session is still open — the app was
killed mid-night), the view presents the
:class:`~seestar_controller.observing.stale_session_dialog.StaleSessionDialog`
(via ``GLib.idle_add``, pumped with the main-context helper) instead of the
raw inline error; its **Resume** button calls the controller's
``resume_stale_session`` and **Close and start new** calls
``close_and_start_new``; other start errors stay inline.

Observing ticket 06 adds the stale-resolution tests: clicking **Resolve**
clears the previous ``_resolved`` at click time (before the slow catalog
lookup), shows a "resolving <name>…" status and gates the **Add** button while
the lookup is in flight, so a quick Resolve->Add can never queue the stale
target; a name mismatch, an in-flight resolve, or a failed resolve -> Add
shows an inline "resolve first"/"still resolving" error and queues nothing;
after the new resolve completes, Add queues the NEW resolved target.
"""

import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest

gi = pytest.importorskip("gi")

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError as exc:  # typelibs missing
    pytest.skip(f"GTK 4.0 / libadwaita typelibs missing: {exc}", allow_module_level=True)

from datetime import UTC

from gi.repository import Gtk

from seestar_controller.catalog.coords import J2000Coord
from seestar_controller.catalog.simbad import NameNotFoundError
from seestar_controller.link.image import Frame
from seestar_controller.link.preview import PreviewFrame, decode_payload
from seestar_controller.observing import (
    EVENT_ADDED,
    ExecutorState,
    ObserveStateError,
    ObserveViewState,
    PlanQueueController,
    QueueEntry,
    StaleSessionError,
)
from seestar_controller.store.sessions import Session
from seestar_controller.ui.connection import ConnectionStatus

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
    except Exception as exc:  # pragma: no cover - backend errors  # noqa: BLE001
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
    assert editor.row_label(1).startswith("RA 10.6800h Dec +41.2700° | mosaic 2°×1° | 10 s | gain 50")
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


def test_add_by_name_requires_a_successful_resolve(gtk, controller, editor):
    # Add with a name that was never resolved: inline "resolve first" error,
    # nothing queued (ticket 06)
    editor.name_entry.set_text("M 42")
    editor.exposure_spin.set_value(30)
    editor.gain_spin.set_value(80)
    editor.add_button.emit("clicked")
    assert editor.row_count() == 0
    assert "resolve" in editor.status_label.get_label()
    assert editor.status_label.has_css_class("error")

    # Resolve looks the name up via the (mocked) catalog and shows the coord
    editor.resolve_button.emit("clicked")
    status = editor.status_label.get_label()
    assert "M 42" in status and "RA 5.5881h" in status and "-5.3911" in status
    assert not editor.status_label.has_css_class("error")

    # Add now stores the RESOLVED entry (name + coord)
    editor.add_button.emit("clicked")
    assert editor.row_count() == 1
    resolved = controller[0]
    assert resolved.name == "M 42" and resolved.coord == COORD_A
    assert resolved.exposure_s == 30.0 and resolved.gain == 80
    assert editor.row_label(0).startswith("M 42 (RA 5.5881h Dec -5.3911°)")
    assert editor.name_entry.get_text() == ""  # fields cleared after a successful add


def test_resolve_button_shows_inline_error_for_unknown_name(gtk, controller, editor):
    editor.name_entry.set_text("NO SUCH OBJECT")
    editor.resolve_button.emit("clicked")
    status = editor.status_label.get_label()
    assert "NO SUCH OBJECT" in status and "NameNotFoundError" in status
    assert editor.status_label.has_css_class("error")
    assert editor.row_count() == 0  # nothing added
    assert editor._resolved is None  # the failed resolve cleared the resolution

    # after a failed resolve, Add with the name errors ("resolve first") and
    # queues nothing — the old coord is never used (ticket 06)
    editor.add_button.emit("clicked")
    assert editor.row_count() == 0
    assert "resolve" in editor.status_label.get_label()
    assert editor.status_label.has_css_class("error")

    # an empty name is an inline error too
    editor.name_entry.set_text("   ")
    editor.resolve_button.emit("clicked")
    assert editor.status_label.has_css_class("error")
    assert editor.row_count() == 0


# --- observing ticket 06: Resolve clears the stale target ------------------------------


def test_add_with_a_name_different_from_the_resolved_one_errors(gtk, controller, editor):
    """A resolution only counts for the exact name it was resolved for: after
    resolving "M 42", retyping "M 31" (no new Resolve) and clicking Add must
    error and queue nothing — the stale M 42 coord is never used."""
    editor.name_entry.set_text("M 42")
    editor.resolve_button.emit("clicked")
    assert "RA 5.5881h" in editor.status_label.get_label()

    editor.name_entry.set_text("M 31")  # name mismatch, no new Resolve
    editor.add_button.emit("clicked")
    assert editor.row_count() == 0
    assert "resolve" in editor.status_label.get_label()
    assert editor.status_label.has_css_class("error")
    assert controller.entries == ()

    # resolving the new name properly, then Add queues M 31 with its own coord
    editor.resolve_button.emit("clicked")
    editor.add_button.emit("clicked")
    assert len(controller) == 1
    assert controller[0].name == "M 31" and controller[0].coord == COORD_B


def test_quick_resolve_then_add_never_queues_the_stale_target(gtk):
    """The acceptance scenario of ticket 06: resolve a target, then re-Resolve
    another name and click Add while the (slow) lookup is still in flight —
    the stale resolution must already be invalidated, so nothing is queued
    then, and the later Add queues the NEW target, never the old one."""
    from seestar_controller.observing import QueueEditor

    in_flight: dict[str, Any] = {}

    def slow_resolver(mapping):
        def resolver(name):
            # mid-lookup: capture the editor state and simulate the user
            # clicking Add while the resolve is in flight
            in_flight["resolved"] = editor._resolved
            in_flight["resolving"] = editor._resolving
            in_flight["add_sensitive"] = editor.add_button.get_sensitive()
            in_flight["status"] = editor.status_label.get_label()
            editor.add_button.emit("clicked")
            in_flight["rows_after_add"] = editor.row_count()
            in_flight["add_status"] = editor.status_label.get_label()
            if name not in mapping:
                raise NameNotFoundError(f"SIMBAD has no object named {name!r}")
            return mapping[name]

        return resolver

    controller = PlanQueueController(
        resolver=slow_resolver({"M 42": COORD_A, "M 31": COORD_B})
    )
    editor = QueueEditor(controller)

    # resolve the first target (the in-flight Add is rejected there too)
    editor.name_entry.set_text("M 42")
    editor.resolve_button.emit("clicked")
    assert editor.row_count() == 0
    assert editor.add_button.get_sensitive()  # gate released after the lookup

    # re-Resolve another name; the Add click lands mid-lookup this time
    editor.name_entry.set_text("M 31")
    editor.resolve_button.emit("clicked")
    assert in_flight["resolved"] is None  # stale resolution already cleared
    assert in_flight["resolving"] is True
    assert in_flight["add_sensitive"] is False  # Add gated while resolving
    assert "resolving" in in_flight["status"]
    assert in_flight["rows_after_add"] == 0  # the quick Add queued nothing
    assert "resolving" in in_flight["add_status"]
    assert controller.entries == ()

    # the lookup finished: Add now queues the NEW target, never the old one
    assert editor.add_button.get_sensitive()
    editor.add_button.emit("clicked")
    assert len(controller) == 1
    assert controller[0].name == "M 31" and controller[0].coord == COORD_B
    assert editor.row_label(0).startswith("M 31 (RA 0.7120h Dec +41.2700°)")


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
    # the editor edits the shared queue (Resolve first, then Add — ticket 06)
    view.queue_editor.name_entry.set_text("M 31")
    view.queue_editor.resolve_button.emit("clicked")
    view.queue_editor.add_button.emit("clicked")
    assert len(controller) == 1
    assert controller[0].name == "M 31" and controller[0].coord == COORD_B

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
    editor.resolve_button.emit("clicked")
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
        self.capture = None  # ticket 04: the SessionCapture, when a night ran

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

    def resume_stale_session(self, project=None, session_id=None, **kwargs):
        """Ticket 05: the stale-session dialog's Resume action."""
        self.calls.append(("resume_stale_session", project, session_id, kwargs))
        if self.start_error is not None:
            raise self.start_error
        return SimpleNamespace()

    def close_and_start_new(self, project=None, session_id=None, **kwargs):
        """Ticket 05: the stale-session dialog's Close-and-start-new action."""
        self.calls.append(("close_and_start_new", project, session_id, kwargs))
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
    # connected + project set + idle: start and park enabled, pause not
    assert widget.start_button.get_sensitive()
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


# --- observing ticket 04: the session log dialog + the Observe-view button --------


def _capture_for_session(tmp_path):
    """A real (pure) SessionCapture over a tmp project with one open session
    — the device is a bare stub: the dialog only reads the log + frames."""
    from seestar_controller.observing import SessionCapture
    from seestar_controller.store.project import create_project
    from seestar_controller.store.sessions import start_session

    project = create_project("Log Night", root=tmp_path / "projects")
    session = start_session(project)
    capture = SessionCapture(object(), project, session)
    return project, session, capture


def test_observe_view_has_a_session_log_button(gtk, controller, observe_stub):
    from seestar_controller.ui.views import build_view

    view = build_view(
        "observe", queue_controller=controller, observe_controller=observe_stub
    )
    widget = view.observe_widget
    assert widget.log_button is not None
    assert widget.log_button.get_label() == "Session log"
    assert widget.log_dialog is None


def test_observe_view_log_button_without_capture_shows_status(gtk, controller, observe_stub):
    from seestar_controller.ui.views import build_view

    view = build_view(
        "observe", queue_controller=controller, observe_controller=observe_stub
    )
    widget = view.observe_widget
    assert observe_stub.capture is None
    widget.log_button.emit("clicked")
    assert widget.log_dialog is None  # no dialog without a capture
    assert "no session yet" in widget.status_label.get_label()


def test_observe_view_log_button_opens_the_dialog(gtk, controller, observe_stub, tmp_path):
    from seestar_controller.observing import SessionLogDialog
    from seestar_controller.ui.views import build_view

    _project, _session, capture = _capture_for_session(tmp_path)
    observe_stub.capture = capture
    view = build_view(
        "observe", queue_controller=controller, observe_controller=observe_stub
    )
    widget = view.observe_widget
    # present() needs a root window
    window = Gtk.Window()
    window.set_child(view)
    widget.log_button.emit("clicked")
    assert isinstance(widget.log_dialog, SessionLogDialog)
    assert widget.log_dialog.capture is capture
    window.destroy()


def test_session_log_dialog_lists_events_and_refreshes(gtk, tmp_path):
    from seestar_controller.observing import KIND_ERROR, KIND_GOTO, SessionLogDialog

    _project, session, capture = _capture_for_session(tmp_path)
    capture.log.append(KIND_GOTO, "goto RA 5.5000h Dec -5.0000°", source="executor", state="slewing")
    capture.log.append(KIND_ERROR, "error on entry 0: RpcError: boom", source="executor")
    dialog = SessionLogDialog(capture)

    assert dialog.row_count() == len(capture.events()) == 2
    assert session in dialog.summary_label.get_label()
    assert "2 event(s)" in dialog.summary_label.get_label()
    assert "0 frame(s) stored" in dialog.summary_label.get_label()
    assert KIND_GOTO in dialog.row_label(0) and "goto RA 5.5000h" in dialog.row_label(0)
    assert KIND_ERROR in dialog.row_label(1) and "boom" in dialog.row_label(1)
    assert dialog.rows()[1].event.kind == KIND_ERROR  # test hook
    assert dialog.rows()[1].get_child().get_last_child().has_css_class("error")

    # a new event while the dialog is open refreshes it automatically
    capture.log.append(KIND_GOTO, "goto RA 10.0000h Dec +20.0000°", source="executor")
    assert dialog.row_count() == 3
    # the Refresh button rebuilds too
    dialog.refresh_button.emit("clicked")
    assert dialog.row_count() == 3
    dialog.close()


def test_session_log_dialog_summary_counts_stored_frames(gtk, tmp_path):
    from datetime import datetime

    from seestar_controller.link.mock_scope import make_fits_bytes
    from seestar_controller.observing import SessionLogDialog
    from seestar_controller.store.frames import store_frame

    project, session, capture = _capture_for_session(tmp_path)
    dialog = SessionLogDialog(capture)
    assert "0 frame(s) stored (0 sub, 0 stack)" in dialog.summary_label.get_label()
    store_frame(
        project,
        session,
        "sub",
        make_fits_bytes(when=datetime.now(UTC), exposure=10.0, gain=50),
    )
    store_frame(
        project,
        session,
        "stack",
        make_fits_bytes(when=datetime.now(UTC), exposure=10.0, gain=80),
    )
    dialog.refresh()
    assert "2 frame(s) stored (1 sub, 1 stack)" in dialog.summary_label.get_label()
    dialog.close()


# --- observing ticket 05: the stale-session resume/close dialog --------------------------
#
# When "Start night" raises SessionOpenError because a stale session is still
# open (the app was killed mid-night), the Observe view must show the
# StaleSessionDialog (Resume / Close-and-start-new, presented via
# GLib.idle_add) instead of surfacing the raw error inline.

STALE_SESSION = Session(
    id="20261120T220000000000Z",
    started_at="2026-11-20T22:00:00+00:00",
    ended_at=None,
)


def _stale_stub(observe_stub):
    """Make the stub's start_night raise a StaleSessionError (a SessionOpenError)."""
    observe_stub.start_error = StaleSessionError(STALE_SESSION)
    return observe_stub


def _observe_view_with_stale_session(gtk, controller, observe_stub):
    """Build the observe view, click Start night, pump the main loop (the
    dialog is marshalled via GLib.idle_add) and return (view, widget, window,
    dialog)."""
    from seestar_controller.observing import StaleSessionDialog
    from seestar_controller.ui.views import build_view

    _stale_stub(observe_stub)
    view = build_view(
        "observe", queue_controller=controller, observe_controller=observe_stub
    )
    widget = view.observe_widget
    window = Gtk.Window()
    window.set_child(view)
    widget.start_button.emit("clicked")
    _pump_main_context()  # the dialog is presented via GLib.idle_add
    dialog = widget.stale_dialog
    assert isinstance(dialog, StaleSessionDialog)
    return view, widget, window, dialog


def test_start_button_with_stale_open_session_shows_the_dialog(gtk, controller, observe_stub):
    _view, widget, window, dialog = _observe_view_with_stale_session(
        gtk, controller, observe_stub
    )
    assert dialog.session is STALE_SESSION
    label = dialog.message_label.get_label()
    assert "20261120T220000000000Z" in label
    assert "2026-11-20 22:00:00 UTC" in label
    assert "still open" in label
    assert dialog.resume_button.get_label() == "Resume"
    assert dialog.close_button.get_label() == "Close and start new"
    # the raw SessionOpenError is NOT surfaced inline
    assert "SessionOpenError" not in widget.status_label.get_label()
    window.destroy()


def test_stale_session_dialog_resume_button_resumes_the_session(gtk, controller, observe_stub):
    _view, widget, window, dialog = _observe_view_with_stale_session(
        gtk, controller, observe_stub
    )
    dialog.resume_button.emit("clicked")
    _pump_main_context()  # the "closed" signal may arrive via the main loop
    assert (
        "resume_stale_session",
        observe_stub.project,
        STALE_SESSION.id,
        {},
    ) in observe_stub.calls
    assert widget.stale_dialog is None  # the dialog closed itself
    window.destroy()


def test_stale_session_dialog_close_button_closes_and_starts_new(gtk, controller, observe_stub):
    _view, widget, window, dialog = _observe_view_with_stale_session(
        gtk, controller, observe_stub
    )
    dialog.close_button.emit("clicked")
    _pump_main_context()  # the "closed" signal may arrive via the main loop
    assert (
        "close_and_start_new",
        observe_stub.project,
        STALE_SESSION.id,
        {},
    ) in observe_stub.calls
    assert widget.stale_dialog is None
    window.destroy()


def test_stale_session_dialog_action_error_shows_inline(gtk, controller, observe_stub):
    """A failing dialog action (e.g. the close raced) shows inline, no crash."""
    _view, widget, window, dialog = _observe_view_with_stale_session(
        gtk, controller, observe_stub
    )
    observe_stub.start_error = ObserveStateError("close_session failed")
    dialog.resume_button.emit("clicked")
    assert "close_session failed" in widget.status_label.get_label()
    assert widget.status_label.has_css_class("error")
    window.destroy()


def test_start_button_non_session_error_still_shows_inline(gtk, controller, observe_stub):
    """Only SessionOpenError opens the dialog; other start errors stay inline."""
    from seestar_controller.ui.views import build_view

    observe_stub.start_error = ObserveStateError("a night is already running")
    view = build_view(
        "observe", queue_controller=controller, observe_controller=observe_stub
    )
    widget = view.observe_widget
    window = Gtk.Window()
    window.set_child(view)
    widget.start_button.emit("clicked")
    _pump_main_context()
    assert widget.stale_dialog is None
    assert "already running" in widget.status_label.get_label()
    assert widget.status_label.has_css_class("error")
    window.destroy()


# --- layout regression: night controls must stay on-screen ---------------------------------
#
# The night-controls FlowBox (and the queue editor's controls FlowBox) must
# keep the default halign=FILL. With halign=CENTER the parent Box allocates
# only the FlowBox's natural width (ONE button) while the height was
# negotiated at the full pane width, so the wrapped lines overflowed the
# FlowBox's allocation and Skip/Stop/Park/Session log were rendered below the
# window — clipped off-screen and unreachable (the reported bug).


def _pump_main_context(rounds: int = 40) -> None:
    """Iterate the default main context until idle, so pending map/resize/
    size-allocate events are processed (no display server round-trip needed
    beyond what the WM already sent)."""
    from gi.repository import GLib

    ctx = GLib.MainContext.default()
    for _ in range(rounds):
        while ctx.pending():
            ctx.iteration(False)


def _find_flowbox(widget):
    """The first Gtk.FlowBox among ``widget``'s direct children (or None)."""
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.FlowBox):
            return child
        child = child.get_next_sibling()
    return None


def test_night_controls_flowbox_fills_the_pane(gtk, controller, observe_stub):
    """The night-controls FlowBox keeps halign=FILL (the default): the parent
    Box then allocates the full pane width, so the buttons wrap within the
    pane and the negotiated height matches the wrapped lines."""
    from seestar_controller.ui.views import build_view

    view = build_view(
        "observe", queue_controller=controller, observe_controller=observe_stub
    )
    widget = view.observe_widget
    left = widget.paned.get_start_child()
    flow = _find_flowbox(left)
    assert flow is not None
    assert flow.props.halign == Gtk.Align.FILL
    # all six night buttons live in that FlowBox
    buttons = [flow.get_child_at_index(i) for i in range(6)]
    assert all(b is not None for b in buttons)


def test_queue_editor_controls_flowbox_fills_the_pane(gtk, editor):
    """Same guard for the queue editor's add-area controls FlowBox."""
    flow = _find_flowbox(editor)
    assert flow is not None
    assert flow.props.halign == Gtk.Align.FILL


def test_observe_view_night_buttons_fit_a_laptop_screen(gtk, controller, observe_stub):
    """Mapped-layout regression test on a 1280x720 laptop screen: every night
    button (Start/Pause/Skip/Stop/Park/Session log) must lie INSIDE the page's
    content allocation — the page wraps its content in a vertical
    Gtk.ScrolledWindow, so a button inside the content is always reachable by
    scrolling, never clipped off-screen. With the old halign=CENTER FlowBox
    the wrapped buttons landed below the content allocation (off-screen)."""
    from seestar_controller.ui.views import build_view

    view = build_view(
        "observe", queue_controller=controller, observe_controller=observe_stub
    )
    widget = view.observe_widget
    window = Gtk.Window()
    window.set_default_size(1280, 720)  # small laptop screen
    window.set_child(view)
    window.present()
    _pump_main_context()
    _pump_main_context()

    # the controls FlowBox got the full pane width (wraps within the pane)
    left = widget.paned.get_start_child()
    flow = _find_flowbox(left)
    assert flow is not None
    assert flow.get_allocated_width() >= left.get_allocated_width() - 24 - 1  # 12px margins

    # every night button is inside the page content allocation (reachable via
    # the page's scrolled window), not rendered outside it
    content = view.content
    alloc = content.get_allocation()
    assert alloc.width > 0 and alloc.height > 0
    for name in (
        "start_button",
        "pause_button",
        "skip_button",
        "stop_button",
        "park_button",
        "log_button",
    ):
        button = getattr(widget, name)
        ok, rect = button.compute_bounds(content)
        assert ok, name
        x, y, w, h = rect.get_x(), rect.get_y(), rect.get_width(), rect.get_height()
        assert -1 <= x and x + w <= alloc.width + 1, (name, x, w, alloc.width)
        assert -1 <= y and y + h <= alloc.height + 1, (name, y, h, alloc.height)

    # the page itself scrolls vertically instead of demanding full height
    assert isinstance(view.scrolled, Gtk.ScrolledWindow)
    assert view.scrolled.props.vscrollbar_policy == Gtk.PolicyType.AUTOMATIC
    window.destroy()
