"""The GTK4/libadwaita application shell — the WIDGET layer of the ui package.

This module imports ``gi`` at module top (widget module — the pure layer
:mod:`seestar_controller.ui.state` never imports this). Entry points:

- ``seestar-controller gui`` (the CLI imports this module lazily, so the CLI
  still works on machines without GTK), or
- ``uv run python -m seestar_controller.ui.app``.

DESKTOP REQUIREMENTS (PyGObject is deliberately NOT a pip dependency — it
cannot be built without the system GTK headers, and this sandbox has no GTK
at all, see docs/wiki/assumptions.md). On the desktop machine install the
system packages::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

Window layout (documented choice): an :class:`Adw.ApplicationWindow` whose
child is an :class:`Adw.ToolbarView` with an :class:`Adw.HeaderBar` on top
and an :class:`Adw.NavigationSplitView` below. **Sidebar choice:** a
:class:`Gtk.ListBox` (one row per view) in the split view's sidebar — chosen
over :class:`Adw.NavigationView` because a split view keeps the sidebar
permanently visible on a wide desktop window, while NavigationView is the
drill-down pattern for narrow/mobile layouts. The content side is an
:class:`Adw.ViewStack` with one page per view; selecting a sidebar row
switches the stack page and updates the pure-layer
:class:`~seestar_controller.ui.state.AppState` (single source of truth for
the current view).

Dark theme: :func:`apply_dark_theme` drives
:meth:`Adw.StyleManager.set_color_scheme` — ``FORCE_DARK`` when the
settings flag is on (default), ``FORCE_LIGHT`` when off.

Window geometry: restored on start (``set_default_size`` + ``move`` —
``move`` is honored on X11/XFCE, the user's desktop; a Wayland compositor
may ignore it) and captured on ``close-request`` (``get_width`` /
``get_height`` / ``get_position``) into the pure layer, which persists it
atomically to the settings file.

Settings (ticket 02): the application owns a
:class:`~seestar_controller.ui.settings.SettingsController` over the loaded
:class:`~seestar_controller.ui.state.AppState` (startup load already happens
in :func:`~seestar_controller.ui.state.load_state`; the scope IP it holds is
what ticket 03's connect will use). The header bar carries a menu button
whose **Settings** item activates the window-level ``win.settings`` action,
which presents a :class:`~seestar_controller.ui.settings_dialog.SettingsDialog`
bound to that controller.

Connection (ticket 03): the application owns a
:class:`~seestar_controller.ui.connection.ConnectionController` over the same
settings, with its ``invoke_on_main`` bound to :func:`GLib.idle_add` (see
:func:`_invoke_on_main`) so the worker-thread results and the device
lifecycle events are applied on the GLib main loop. The header bar carries a
:class:`~seestar_controller.ui.connection_panel.ConnectionPanel`
(``pack_start``) — a Connect/Disconnect button + status label showing
disconnected/connecting/connected/reconnecting/error, and the Scope's
current state (mode, target) once connected.

Notifications (ticket 04): the application owns a shared
:class:`~seestar_controller.ui.notifications.NotificationBus` plus a
:class:`~seestar_controller.ui.notifications.NotificationController`
subscribed to the connection controller (and, once connected, to the
device's pushed events), so connection loss, scope errors, and stack events
surface as notifications. The window wraps its whole content in an
:class:`Adw.ToastOverlay`; a
:class:`~seestar_controller.ui.toast.NotificationToasts` presenter turns bus
notifications into non-modal :class:`Adw.Toast`\\ s (marshalled onto the
main loop via the same ``invoke_on_main``). The header bar carries a
notifications button (``win.notifications`` action) that opens the
user-invoked :class:`~seestar_controller.ui.notification_log.NotificationLogDialog`
listing the bus log with a Clear button — background events never open
modal dialogs.

Observing (ticket 01): the application owns a shared
:class:`~seestar_controller.observing.queue_controller.PlanQueueController`
(tonight's plan queue, in memory for the session — not persisted to disk yet),
and the window passes it to the Observe view, whose
:class:`~seestar_controller.observing.queue_editor.QueueEditor` edits it.
"""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from seestar_controller.observing import PlanQueueController  # noqa: E402

from .connection import ConnectionController  # noqa: E402
from .connection_panel import ConnectionPanel  # noqa: E402
from .notification_log import NotificationLogDialog  # noqa: E402
from .notifications import NotificationBus, NotificationController  # noqa: E402
from .settings import SettingsController  # noqa: E402
from .settings_dialog import SettingsDialog  # noqa: E402
from .state import (  # noqa: E402
    AppState,
    SettingsValidationError,
    load_state,
)
from .toast import NotificationToasts  # noqa: E402
from .views import build_view  # noqa: E402

#: GApplication id.
APP_ID = "io.github.seestar-controller"

#: Window title.
APP_NAME = "Seestar Controller"

#: Sidebar minimum width (pixels).
SIDEBAR_WIDTH = 180


def _invoke_on_main(callback) -> int:
    """Marshal a pure-layer callback onto the GLib main loop.

    This is the ``invoke_on_main`` the app binds to the pure
    :class:`~seestar_controller.ui.connection.ConnectionController`: the
    blocking ``Device.connect`` runs on a worker thread and its result (and
    the device lifecycle events) are applied here, on the main thread.
    """
    return GLib.idle_add(callback)


def apply_dark_theme(dark: bool) -> None:
    """Apply the dark-theme flag via the libadwaita style manager.

    ``dark=True`` -> ``FORCE_DARK`` (the default — night-time use),
    ``dark=False`` -> ``FORCE_LIGHT``.
    """
    manager = Adw.StyleManager.get_default()
    manager.set_color_scheme(
        Adw.ColorScheme.FORCE_DARK if dark else Adw.ColorScheme.FORCE_LIGHT
    )


def _capture_geometry(window: Gtk.Window) -> dict:
    """Read the current window geometry: ``{"width", "height", "x", "y"}``.

    ``x``/``y`` are ``None`` when the backend cannot report a position.
    """
    width, height = window.get_width(), window.get_height()
    x = y = None
    get_position = getattr(window, "get_position", None)
    if get_position is not None:
        try:
            x, y = get_position()
        except Exception:
            x = y = None
    return {"width": width, "height": height, "x": x, "y": y}


def _restore_geometry(window: Gtk.Window, geometry: dict) -> None:
    """Apply a saved geometry dict to ``window`` (size always; position when
    set and supported by the backend)."""
    window.set_default_size(geometry["width"], geometry["height"])
    x, y = geometry.get("x"), geometry.get("y")
    move = getattr(window, "move", None)
    if x is not None and y is not None and move is not None:
        move(x, y)  # honored on X11/XFCE; Wayland may ignore it


class SeestarWindow(Adw.ApplicationWindow):
    """The main window: header bar (connection panel + notifications log
    button + settings menu) + sidebar (ListBox) + view stack, all wrapped in
    an :class:`Adw.ToastOverlay` (ticket 04 — non-modal notification toasts).

    The pure-layer :class:`~seestar_controller.ui.state.AppState` is the
    single source of truth: the window renders it (initial view, geometry,
    theme) and writes back to it (view selection, geometry on close).
    """

    def __init__(
        self,
        app: Adw.Application,
        state: AppState,
        settings_controller: SettingsController | None = None,
        connection_controller: ConnectionController | None = None,
        notification_bus: NotificationBus | None = None,
        queue_controller: PlanQueueController | None = None,
    ) -> None:
        super().__init__(application=app, title=APP_NAME)
        self._state = state
        # One shared controller: the settings dialog edits through it and the
        # window listens to it (observer), so the UI refreshes on change.
        self._settings_controller = (
            settings_controller
            if settings_controller is not None
            else getattr(app, "settings_controller", None)
            or SettingsController(state)
        )
        # Ticket 03: one shared connection controller; the header panel
        # observes it (its invoke_on_main is GLib.idle_add, so worker-thread
        # results land on the main loop).
        self._connection_controller = (
            connection_controller
            if connection_controller is not None
            else getattr(app, "connection_controller", None)
            or ConnectionController(self._settings_controller, invoke_on_main=_invoke_on_main)
        )
        # Ticket 04: one shared notification bus; the notification controller
        # translates connection snapshots + device events into bus
        # notifications (the toasts + log read from this bus).
        self._notification_bus = (
            notification_bus
            if notification_bus is not None
            else getattr(app, "notification_bus", None)
            or NotificationBus()
        )
        self._notification_controller = (
            getattr(app, "notification_controller", None)
            or NotificationController(
                self._notification_bus,
                self._connection_controller,
                invoke_on_main=_invoke_on_main,
            )
        )
        # Observing ticket 01: one shared plan-queue controller; the Observe
        # view's queue editor edits through it.
        self._queue_controller = (
            queue_controller
            if queue_controller is not None
            else getattr(app, "queue_controller", None)
            or PlanQueueController()
        )

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=APP_NAME))
        # Connection panel (ticket 03): connect button + status, left side.
        self._connection_panel = ConnectionPanel(self._connection_controller)
        header.pack_start(self._connection_panel)
        # Notifications log button (ticket 04): opens the log dialog via the
        # win.notifications action (packed before Settings, which stays
        # rightmost).
        notifications_action = Gio.SimpleAction.new("notifications", None)
        notifications_action.connect("activate", self._on_notifications_action)
        self.add_action(notifications_action)
        self._notifications_button = Gtk.Button(
            icon_name="view-list-symbolic",
            tooltip_text="Notifications",
        )
        self._notifications_button.connect(
            "clicked", lambda *_args: self.activate_action("win.notifications", None)
        )
        header.pack_end(self._notifications_button)
        # Settings menu button (ticket 02): one item activating win.settings.
        settings_action = Gio.SimpleAction.new("settings", None)
        settings_action.connect("activate", self._on_settings_action)
        self.add_action(settings_action)
        menu = Gio.Menu()
        menu.append("Settings", "win.settings")
        self._settings_button = Gtk.MenuButton(
            menu_model=menu,
            icon_name="emblem-system-symbolic",
            tooltip_text="Settings",
        )
        header.pack_end(self._settings_button)
        toolbar_view.add_top_bar(header)

        # Content: one stack page per view.
        self._stack = Adw.ViewStack()
        for view in state.views:
            self._stack.add_titled(
                build_view(view.id, queue_controller=self._queue_controller),
                view.id,
                view.title,
            )

        # Sidebar: a ListBox with one row per view (documented choice, see
        # module docstring).
        self._rows: dict[str, Gtk.ListBoxRow] = {}
        sidebar = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        sidebar.add_css_class("navigation-sidebar")
        for view in state.views:
            row = Gtk.ListBoxRow(child=Gtk.Label(label=view.title, xalign=0.0))
            row.view_id = view.id  # type: ignore[attr-defined]
            self._rows[view.id] = row
            sidebar.append(row)
        sidebar.connect("row-selected", self._on_row_selected)
        scrolled = Gtk.ScrolledWindow(
            child=sidebar,
            min_content_width=SIDEBAR_WIDTH,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )

        split = Adw.NavigationSplitView()
        split.set_sidebar(scrolled)
        split.set_content(self._stack)
        toolbar_view.set_content(split)
        # Ticket 04: the whole content sits inside an Adw.ToastOverlay, so
        # bus notifications surface as non-modal toasts over the current view.
        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(toolbar_view)
        self.set_content(self._toast_overlay)
        # Ticket 04: the bus -> toast presenter (bus callbacks may come from
        # worker threads, so they are marshalled via GLib.idle_add here).
        self._toast_presenter = NotificationToasts(
            self._notification_bus,
            self._toast_overlay,
            invoke_on_main=_invoke_on_main,
            on_open_log=self._on_notifications_action,
        )

        # Render the persisted state: geometry, theme, initial view.
        _restore_geometry(self, state.window_geometry())
        apply_dark_theme(state.dark_theme)
        self.select_view(state.current_view_id)

        # Reflect settings changes in the header (scope IP in the tooltip;
        # ticket 03's connect reads the same controller).
        self._settings_controller.add_listener(self._on_settings_changed)
        self._on_settings_changed(self._settings_controller.values)

        self.connect("close-request", self._on_close_request)

    @property
    def state(self) -> AppState:
        """The pure-layer app state backing this window."""
        return self._state

    @property
    def settings_controller(self) -> SettingsController:
        """The shared settings controller (dialog edits + observer)."""
        return self._settings_controller

    @property
    def connection_controller(self) -> ConnectionController:
        """The shared connection controller (ticket 03 — the header panel
        observes it)."""
        return self._connection_controller

    @property
    def connection_panel(self) -> ConnectionPanel:
        """The header-bar connection panel (connect button + status label)."""
        return self._connection_panel

    @property
    def notification_bus(self) -> NotificationBus:
        """The shared notification bus (ticket 04 — toasts + log read it)."""
        return self._notification_bus

    @property
    def notification_controller(self) -> NotificationController:
        """The shared notification controller (subscribed to the connection
        controller; pushes to the bus)."""
        return self._notification_controller

    @property
    def toast_overlay(self) -> Adw.ToastOverlay:
        """The toast overlay wrapping the window content (ticket 04)."""
        return self._toast_overlay

    @property
    def toast_presenter(self) -> NotificationToasts:
        """The bus -> toast presenter (ticket 04)."""
        return self._toast_presenter

    @property
    def notifications_button(self) -> Gtk.Button:
        """The header-bar notifications (log) button (ticket 04)."""
        return self._notifications_button

    @property
    def view_stack(self) -> Adw.ViewStack:
        """The view stack holding the five view pages."""
        return self._stack

    @property
    def queue_controller(self) -> PlanQueueController:
        """The shared plan-queue controller (observing ticket 01 — the
        Observe view's queue editor edits through it)."""
        return self._queue_controller

    def select_view(self, view_id: str) -> None:
        """Select a view: highlight its sidebar row (which switches the stack
        page and updates the state via the row-selected handler)."""
        # Validate via the pure layer FIRST: an unknown id raises
        # UnknownViewError before any widget lookup (review F1).
        view = self._state.select_view(view_id)
        row = self._rows[view.id]
        self._stack.set_visible_child_name(view.id)
        sidebar = row.get_parent()
        if sidebar is not None:
            sidebar.select_row(row)

    def _on_row_selected(self, sidebar: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        view_id = row.view_id  # type: ignore[attr-defined]
        self._stack.set_visible_child_name(view_id)
        self._state.select_view(view_id)

    def _on_settings_action(self, *_args) -> None:
        """Open the settings dialog (the ``win.settings`` action handler)."""
        self._settings_dialog = SettingsDialog(self._settings_controller)
        self._settings_dialog.present(self)

    def _on_notifications_action(self, *_args) -> None:
        """Open the notification log dialog (the ``win.notifications`` action
        handler — user-invoked only; background events never open it)."""
        self._notification_log_dialog = NotificationLogDialog(self._notification_bus)
        self._notification_log_dialog.present(self)

    def _on_settings_changed(self, values: dict[str, str]) -> None:
        """Observer: settings changed -> reflect the scope IP in the header."""
        scope_ip = values.get("scope_ip", "")
        self._settings_button.set_tooltip_text(
            f"Settings (scope: {scope_ip})" if scope_ip else "Settings"
        )

    def _on_close_request(self, *_args) -> bool:
        """Persist geometry + state on close; never block closing."""
        geometry = _capture_geometry(self)
        try:
            self._state.set_window_geometry(
                geometry["width"], geometry["height"], geometry["x"], geometry["y"]
            )
        except SettingsValidationError:
            pass  # a degenerate size (e.g. 0 before realize) is not persisted
        self._state.save()
        return False  # allow the close


class SeestarApplication(Adw.Application):
    """The application: owns the pure-layer :class:`AppState` (loaded from
    the settings file) and opens one :class:`SeestarWindow` on activate."""

    def __init__(
        self,
        *,
        settings_path: str | None = None,
        env: dict | None = None,
    ) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        # Startup: load the settings file (missing/corrupt -> defaults, never
        # raises) and wrap it in the shared settings controller.
        self._state = load_state(settings_path, env=env)
        self.settings_controller = SettingsController(self._state)
        # Ticket 03: the shared connection controller over the same settings;
        # worker-thread results are marshalled onto the GLib main loop.
        self.connection_controller = ConnectionController(
            self.settings_controller, invoke_on_main=_invoke_on_main
        )
        # Ticket 04: the shared notification bus + the controller translating
        # connection snapshots and device events into notifications (the
        # window's toast overlay + log dialog read from this bus).
        self.notification_bus = NotificationBus()
        self.notification_controller = NotificationController(
            self.notification_bus,
            self.connection_controller,
            invoke_on_main=_invoke_on_main,
        )
        # Observing ticket 01: the shared plan-queue controller (tonight's
        # plan, in memory for the session — not persisted to disk yet); the
        # window passes it to the Observe view's queue editor.
        self.queue_controller = PlanQueueController()

    @property
    def state(self) -> AppState:
        """The pure-layer app state (loaded from the settings file)."""
        return self._state

    def do_startup(self) -> None:
        super().do_startup()
        apply_dark_theme(self._state.dark_theme)

    def do_activate(self) -> None:
        window = SeestarWindow(self, self._state, self.settings_controller)
        window.present()


def run(argv: list[str] | None = None) -> int:
    """Run the GTK application; returns the exit status."""
    app = SeestarApplication()
    return app.run(sys.argv if argv is None else list(argv))


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m seestar_controller.ui.app``."""
    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
