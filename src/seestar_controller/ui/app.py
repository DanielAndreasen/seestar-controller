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
atomically to the settings file. The window always opens **maximized**
("full screen (maximized)", NOT fullscreen): ``maximize()`` is requested in
the window constructor *after* the geometry restore (so the restored size
stays only the default (unmaximized) size and a saved smaller size cannot
un-maximize the window on open) and re-requested once on the first ``map``
(some WMs/compositors ignore a pre-map request); the geometry is still
captured on close for reference.

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

Observing (tickets 01 + 03): the application owns a shared
:class:`~seestar_controller.observing.queue_controller.PlanQueueController`
(tonight's plan queue, in memory for the session — not persisted to disk yet)
and a shared
:class:`~seestar_controller.observing.observe_controller.ObserveController`
binding connection + queue + night executor + preview stream + notification
bus (its ``invoke_on_main`` is :func:`GLib.idle_add`, like the connection
controller's). The window passes both to the Observe view, whose queue
editor edits the queue and whose preview/progress/controls render the
observe controller's snapshots.

Image-enhance (tickets 01 + 03): the application owns a shared
:class:`~seestar_controller.enhance.viewer_controller.ViewerController`
(the current project's stack list + open stack + tone-mapped preview) and a
shared
:class:`~seestar_controller.enhance.editor_controller.EditorController`
(the editing state of the open stack — current pipeline, bounded undo/redo,
debounced worker-thread re-renders marshalled onto the GLib main loop via
:func:`_invoke_on_main`, like the connection controller's). The window
passes both to the Enhance view, whose
:class:`~seestar_controller.enhance.enhance_view.EnhanceEditor` renders the
snapshots and binds the sliders/buttons to the editor controller. No project
is selected at startup (the Projects view is a later ticket), so the
controllers start empty and the view shows a "no project" placeholder until
:meth:`~seestar_controller.enhance.viewer_controller.ViewerController.set_project`
is called.

Photometry (ticket 01): the application owns a shared
:class:`~seestar_controller.photometry.picking.PickController` following the
shared viewer controller (every stack opened on the Enhance view becomes the
pick session's current image). The window passes both to the Photometry view,
whose :class:`~seestar_controller.photometry.star_picker.StarPickerView`
renders the clickable preview with the aperture overlay, the role/aperture
controls, the picks list and the Save picks button (persisting the picks into
the project via :mod:`seestar_controller.store.stars`).

Photometry (ticket 02): the application also owns a shared
:class:`~seestar_controller.photometry.suggest.SuggestionController` — the
catalog assist for comps — bound to the shared pick controller and configured
with the comp-search radius read from the settings
(:func:`seestar_controller.ui.settings.comp_search_radius_deg`; a settings
listener keeps it in sync live). The window passes it to the Photometry view,
whose :class:`~seestar_controller.photometry.suggestions_panel.SuggestionsPanel`
lists the standard stars near the target and adds one as a comp pick.

Photometry (ticket 04): the application also owns a shared
:class:`~seestar_controller.photometry.run.RunController` — the run + results
controller — bound to the shared pick controller (its picks are measured; the
stored picks are the fallback) with its ``invoke_on_main`` bound to
:func:`GLib.idle_add`, so worker-thread runs marshal their store writes and
listener notifications onto the GLib main loop. The window passes it to the
Photometry view, whose
:class:`~seestar_controller.photometry.results_view.ResultsPanel` runs the
measurements over a chosen session on a worker thread (progress shown) and
shows the per-frame table + mag-vs-jd scatter plot with outlier flagging.

Period-analysis (ticket 01): the application also owns a shared
:class:`~seestar_controller.analysis.lightcurve.LightcurveController` — the
lightcurve view's controller — following the shared viewer controller (its
project is adopted, so the lightcurve follows the currently open project)
with its ``invoke_on_main`` bound to :func:`GLib.idle_add`, like the other
controllers. The window passes it to the Lightcurve view, whose
:class:`~seestar_controller.analysis.lightcurve_view.LightcurveView` renders
the mag-vs-JD plot (one color per session + legend, error bars, excluded
points marked, inverted mag axis) with zoom/pan driven by the pure transform
math.

Period-analysis (ticket 03): the application also owns a shared
:class:`~seestar_controller.analysis.fold.PeriodController` — the period
selection + phase-fold controller — bound to that same shared lightcurve
controller (so it follows the open project and its points), with its
``invoke_on_main`` bound to :func:`GLib.idle_add` like the others. The window
passes it to the Lightcurve view, whose
:class:`~seestar_controller.analysis.period_view.PeriodView` renders the
periodogram (clickable top-N peaks), the manual period entry, and the
phase-folded lightcurve; every selection is persisted into the project's
``project.json`` by the controller.

Frame-quality (ticket 02): the application also owns a shared
:class:`~seestar_controller.frame_quality.QualityController` — the
session-quality trends controller — following the shared viewer controller
(its project is adopted, so the quality trends follow the currently open
project) with its ``invoke_on_main`` bound to :func:`GLib.idle_add`, like the
other controllers. The window passes it to the Lightcurve view, whose
:class:`~seestar_controller.frame_quality.quality_view.SessionQualityView`
renders the session selector, the per-session summary card (median FWHM,
rejected percent) and the three trend plots (FWHM / background / airmass vs
time).

Projects (projects-view tickets 01 + 02): the application owns a shared
:class:`~seestar_controller.ui.project_controller.ProjectController` over the
same :class:`~seestar_controller.ui.state.AppState` (project root from the
settings — unset falls back to the store default ``~/SeestarProjects``,
created on demand; ``last_project`` persisted and restored on startup). The
window passes it to the Projects view (list/create/open) and to the Observe
view, and attaches it to the observe / viewer / picker / run / lightcurve /
period / quality controllers, so every current-project change is pushed to
all of them via their ``set_project``. Creating a project in the Projects view
therefore makes it current everywhere (unblocking Observe's "Start night"
once the scope is connected) and persists it as ``last_project``; an
unusable (e.g. unwritable) project root surfaces as an inline error with a
hint pointing to Settings, and a changed ``project_root`` re-lists the
projects under the new root.
"""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk

from seestar_controller.analysis import (
    LightcurveController,
    PeriodController,
)
from seestar_controller.astrometry import DriftController, SolveController
from seestar_controller.enhance import EditorController, ViewerController
from seestar_controller.frame_quality import QualityController
from seestar_controller.gaia import CrossmatchController
from seestar_controller.hr_diagram import HrDiagramController
from seestar_controller.moving_objects import MoverController
from seestar_controller.observing import (
    ObserveController,
    PlanQueueController,
)
from seestar_controller.photometry import (
    PickController,
    RunController,
    SuggestionController,
)
from seestar_controller.variable_scan import ScanController

from .connection import ConnectionController
from .connection_panel import ConnectionPanel
from .notification_log import NotificationLogDialog
from .notifications import NotificationBus, NotificationController
from .project_controller import ProjectController
from .settings import SettingsController, comp_search_radius_deg
from .settings_dialog import SettingsDialog
from .state import (
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    AppState,
    SettingsValidationError,
    load_state,
)
from .toast import NotificationToasts
from .views import build_view

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


def _shared_from_app(app, name: str, make_default):
    """Read a shared controller off ``app``, falling back to ``make_default()``.

    The fallback must not rely on truthiness: some shared objects define
    ``__len__`` (an empty :class:`NotificationBus` or plan queue is falsy), so
    ``getattr(app, name, None) or default`` would wrongly build a new one.
    """
    value = getattr(app, name, None)
    return value if value is not None else make_default()


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
    if width <= 0 or height <= 0:
        # not mapped yet: fall back to the default size instead of 0x0
        width, height = window.get_default_size()
    x = y = None
    get_position = getattr(window, "get_position", None)
    if get_position is not None:
        try:
            x, y = get_position()
        except Exception:  # noqa: BLE001
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
        observe_controller: ObserveController | None = None,
        viewer_controller: ViewerController | None = None,
        editor_controller: EditorController | None = None,
        picker_controller: PickController | None = None,
        suggest_controller: SuggestionController | None = None,
        run_controller: RunController | None = None,
        lightcurve_controller: LightcurveController | None = None,
        period_controller: PeriodController | None = None,
        quality_controller: QualityController | None = None,
        mover_controller: MoverController | None = None,
        crossmatch_controller: CrossmatchController | None = None,
        hr_diagram_controller: HrDiagramController | None = None,
        scan_controller: ScanController | None = None,
        solve_controller: SolveController | None = None,
        drift_controller: DriftController | None = None,
        project_controller: ProjectController | None = None,
    ) -> None:
        super().__init__(application=app, title=APP_NAME)
        self._state = state
        # One shared controller: the settings dialog edits through it and the
        # window listens to it (observer), so the UI refreshes on change.
        self._settings_controller = (
            settings_controller
            if settings_controller is not None
            else _shared_from_app(app, "settings_controller", lambda: SettingsController(state))
        )
        # Ticket 03: one shared connection controller; the header panel
        # observes it (its invoke_on_main is GLib.idle_add, so worker-thread
        # results land on the main loop).
        self._connection_controller = (
            connection_controller
            if connection_controller is not None
            else _shared_from_app(app, "connection_controller", lambda: ConnectionController(self._settings_controller, invoke_on_main=_invoke_on_main))
        )
        # Ticket 04: one shared notification bus; the notification controller
        # translates connection snapshots + device events into bus
        # notifications (the toasts + log read from this bus).
        self._notification_bus = (
            notification_bus
            if notification_bus is not None
            else _shared_from_app(app, "notification_bus", lambda: NotificationBus())
        )
        self._notification_controller = _shared_from_app(
            app,
            "notification_controller",
            lambda: NotificationController(
                self._notification_bus,
                self._connection_controller,
                invoke_on_main=_invoke_on_main,
            ),
        )
        # Observing ticket 01: one shared plan-queue controller; the Observe
        # view's queue editor edits through it.
        self._queue_controller = (
            queue_controller
            if queue_controller is not None
            else _shared_from_app(app, "queue_controller", lambda: PlanQueueController())
        )
        # Observing ticket 03: one shared observe controller (connection +
        # queue + executor + preview + bus); the Observe view renders its
        # snapshots and binds its controls to it.
        self._observe_controller = (
            observe_controller
            if observe_controller is not None
            else _shared_from_app(app, "observe_controller", lambda: ObserveController(
                self._connection_controller,
                self._queue_controller,
                self._notification_bus,
                invoke_on_main=_invoke_on_main,
            ))
        )
        # Image-enhance ticket 01: one shared viewer controller (project
        # stacks + open stack + preview); the Enhance view renders its
        # snapshots. Starts with no project (the Projects view is a later
        # ticket) — set_project() is called once a project is opened.
        self._viewer_controller = (
            viewer_controller
            if viewer_controller is not None
            else _shared_from_app(app, "viewer_controller", lambda: ViewerController())
        )
        # Image-enhance ticket 03: one shared editor controller over the
        # viewer controller (current pipeline + undo/redo + debounced
        # re-renders); the Enhance view's sliders/buttons bind to it. Its
        # invoke_on_main is GLib.idle_add, so worker-thread render results
        # land on the main loop.
        self._editor_controller = (
            editor_controller
            if editor_controller is not None
            else _shared_from_app(app, "editor_controller", lambda: EditorController(self._viewer_controller, invoke_on_main=_invoke_on_main))
        )
        # Photometry ticket 01: one shared pick controller following the
        # viewer controller (the open stack becomes the pick session's
        # image); the Photometry view renders its snapshots and binds its
        # click/role/aperture/save controls to it.
        self._picker_controller = (
            picker_controller
            if picker_controller is not None
            else _shared_from_app(app, "picker_controller", lambda: PickController(viewer_controller=self._viewer_controller))
        )
        # Photometry ticket 02: one shared catalog-assist controller bound to
        # the pick controller (it follows the target pick); the Photometry
        # view's suggestions panel lists the nearby standard stars and adds
        # them as comp picks. Configured with the comp-search radius from the
        # settings (kept in sync by the settings listener below).
        self._suggest_controller = (
            suggest_controller
            if suggest_controller is not None
            else _shared_from_app(app, "suggest_controller", lambda: SuggestionController(picker_controller=self._picker_controller))
        )
        # Photometry ticket 04: one shared run + results controller bound to
        # the pick controller (its picks are measured; the stored picks are
        # the fallback); the Photometry view's results panel runs sessions on
        # a worker thread and renders the snapshots. Its invoke_on_main is
        # GLib.idle_add, so worker-thread store writes + notifications land on
        # the main loop.
        self._run_controller = (
            run_controller
            if run_controller is not None
            else _shared_from_app(app, "run_controller", lambda: RunController(
                picker_controller=self._picker_controller, invoke_on_main=_invoke_on_main
            ))
        )
        # Period-analysis ticket 01: one shared lightcurve controller
        # following the shared viewer controller (the open project becomes the
        # lightcurve's project); the Lightcurve view renders its snapshots.
        # Its invoke_on_main is GLib.idle_add, so worker-thread store reads +
        # notifications land on the main loop.
        self._lightcurve_controller = (
            lightcurve_controller
            if lightcurve_controller is not None
            else _shared_from_app(app, "lightcurve_controller", lambda: LightcurveController(
                viewer_controller=self._viewer_controller, invoke_on_main=_invoke_on_main
            ))
        )
        # Period-analysis ticket 03: one shared period controller bound to the
        # shared lightcurve controller (it follows the same project and its
        # points); the Lightcurve view's period panel renders its snapshots
        # and binds its peak clicks / manual entry to it. Its invoke_on_main
        # is GLib.idle_add, so worker-thread store writes + notifications land
        # on the main loop.
        self._period_controller = (
            period_controller
            if period_controller is not None
            else _shared_from_app(app, "period_controller", lambda: PeriodController(self._lightcurve_controller, invoke_on_main=_invoke_on_main))
        )
        # Frame-quality ticket 02: one shared quality controller following the
        # shared viewer controller (the open project becomes the quality
        # trends' project); the Lightcurve view's session-quality section
        # renders its snapshots. Its invoke_on_main is GLib.idle_add, so
        # worker-thread store reads + notifications land on the main loop.
        self._quality_controller = (
            quality_controller
            if quality_controller is not None
            else _shared_from_app(app, "quality_controller", lambda: QualityController(
                viewer_controller=self._viewer_controller, invoke_on_main=_invoke_on_main
            ))
        )
        # Moving-objects ticket 02: one shared mover controller following the
        # shared viewer controller (the open project becomes the mover
        # overlay's project); the Photometry view's mover overlay/blink widget
        # renders its snapshots. Its invoke_on_main is GLib.idle_add, so
        # worker-thread store reads/writes + notifications land on the main
        # loop.
        self._mover_controller = (
            mover_controller
            if mover_controller is not None
            else _shared_from_app(app, "mover_controller", lambda: MoverController(
                viewer_controller=self._viewer_controller, invoke_on_main=_invoke_on_main
            ))
        )
        # Gaia-crossmatch ticket 03: one shared cross-match controller
        # following the shared viewer controller (the stack open in the image
        # viewer becomes the cross-match target); the Enhance view's
        # cross-match overlay panel renders its snapshots. Its invoke_on_main
        # is GLib.idle_add, so worker-thread store reads/writes + the Gaia
        # query results + notifications land on the main loop.
        self._crossmatch_controller = (
            crossmatch_controller
            if crossmatch_controller is not None
            else _shared_from_app(app, "crossmatch_controller", lambda: CrossmatchController(
                viewer_controller=self._viewer_controller, invoke_on_main=_invoke_on_main
            ))
        )
        # Hr-diagram ticket 03: one shared H-R diagram controller following
        # the shared viewer controller (the stack open in the image viewer
        # becomes the diagram target when it is crossmatched); the H-R
        # diagram view renders its snapshots. Its invoke_on_main is
        # GLib.idle_add, so worker-thread store reads/writes + notifications
        # land on the main loop.
        self._hr_diagram_controller = (
            hr_diagram_controller
            if hr_diagram_controller is not None
            else _shared_from_app(app, "hr_diagram_controller", lambda: HrDiagramController(
                viewer_controller=self._viewer_controller, invoke_on_main=_invoke_on_main
            ))
        )
        # Variable-scan ticket 02: one shared scan controller following the
        # shared viewer controller (the open project becomes the scan
        # candidate view's project); the Photometry view's variable-scan
        # panel renders its snapshots. Its invoke_on_main is GLib.idle_add,
        # so worker-thread store reads/writes + notifications land on the
        # main loop.
        self._scan_controller = (
            scan_controller
            if scan_controller is not None
            else _shared_from_app(app, "scan_controller", lambda: ScanController(
                viewer_controller=self._viewer_controller, invoke_on_main=_invoke_on_main
            ))
        )
        # Astrometry ticket 03: one shared solve controller following the
        # shared viewer controller (the stack open in the image viewer is the
        # solve/annotated-export target); the Enhance view's solve panel
        # renders its snapshots and reports failures via show_toast. Its
        # invoke_on_main is GLib.idle_add, so worker-thread store
        # reads/writes + solver results land on the main loop.
        self._solve_controller = (
            solve_controller
            if solve_controller is not None
            else _shared_from_app(app, "solve_controller", lambda: SolveController(
                viewer_controller=self._viewer_controller, invoke_on_main=_invoke_on_main
            ))
        )
        # Astrometry ticket 03: one shared drift controller following the
        # shared viewer controller (the open project's sessions become the
        # drift-plot source); the Observe view's drift plot renders its
        # snapshots.
        self._drift_controller = (
            drift_controller
            if drift_controller is not None
            else _shared_from_app(app, "drift_controller", lambda: DriftController(
                viewer_controller=self._viewer_controller, invoke_on_main=_invoke_on_main
            ))
        )
        # Projects-view ticket 01: the shared project controller. When the app
        # owns one it is used (the app already attached its controllers to it);
        # otherwise (explicit argument, or a standalone window without an app
        # controller) a fresh one is created/attached here, so a
        # current-project change (Projects view create/open) is pushed to all
        # of the window's controllers via their set_project().
        if project_controller is not None:
            self._project_controller = project_controller
            self._attach_project_followers()
        elif getattr(app, "project_controller", None) is not None:
            self._project_controller = app.project_controller
        else:
            self._project_controller = ProjectController(state)
            self._attach_project_followers()

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
                build_view(
                    view.id,
                    queue_controller=self._queue_controller,
                    observe_controller=self._observe_controller,
                    viewer_controller=self._viewer_controller,
                    editor_controller=self._editor_controller,
                    picker_controller=self._picker_controller,
                    suggest_controller=self._suggest_controller,
                    run_controller=self._run_controller,
                    lightcurve_controller=self._lightcurve_controller,
                    period_controller=self._period_controller,
                    quality_controller=self._quality_controller,
                    mover_controller=self._mover_controller,
                    crossmatch_controller=self._crossmatch_controller,
                    hr_diagram_controller=self._hr_diagram_controller,
                    scan_controller=self._scan_controller,
                    solve_controller=self._solve_controller,
                    drift_controller=self._drift_controller,
                    show_toast=self._show_toast,
                    project_controller=self._project_controller,
                    project=self._project_controller.current,
                    app_state=state,
                ),
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
        # NavigationSplitView's sidebar/content must be Adw.NavigationPages,
        # not bare widgets.
        split.set_sidebar(Adw.NavigationPage.new(scrolled, "Views"))
        split.set_content(Adw.NavigationPage.new(self._stack, "View"))
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

        # Render the persisted state: geometry, theme, initial view. A
        # sensible default (unmaximized) size is set first; the persisted
        # geometry then overrides it (1280x800 on a fresh state) together
        # with the position.
        self.set_default_size(DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)
        _restore_geometry(self, state.window_geometry())
        # Always open maximized ("full screen (maximized)", NOT fullscreen):
        # maximize() is requested AFTER the geometry restore, so a saved
        # smaller size stays only the default (unmaximized) size and cannot
        # un-maximize the window on open. Geometry is still captured on
        # close-request (see _on_close_request) for reference.
        self.maximize()
        # Some WMs/compositors (notably Wayland) ignore a maximize request
        # made before the window is mapped, so re-request it once on the
        # first map — the window then really opens maximized and stays that
        # way (the maximize button keeps working because the window's
        # minimum size stays well under a laptop workarea).
        self._maximize_on_map_handler = self.connect("map", self._on_map_maximize)
        apply_dark_theme(state.dark_theme)
        self.select_view(state.current_view_id)

        # Reflect settings changes in the header (scope IP in the tooltip;
        # ticket 03's connect reads the same controller).
        self._settings_controller.add_listener(self._on_settings_changed)
        self._on_settings_changed(self._settings_controller.values)

        self.connect("close-request", self._on_close_request)

    def _on_map_maximize(self, *_args) -> None:
        """One-shot ``map`` handler: re-request maximize on the first map
        (see the constructor) and disconnect itself."""
        self.handler_disconnect(self._maximize_on_map_handler)
        self.maximize()

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
        """The view stack holding the six view pages."""
        return self._stack

    @property
    def queue_controller(self) -> PlanQueueController:
        """The shared plan-queue controller (observing ticket 01 — the
        Observe view's queue editor edits through it)."""
        return self._queue_controller

    @property
    def observe_controller(self) -> ObserveController:
        """The shared observe controller (observing ticket 03 — the Observe
        view renders its snapshots and binds its controls to it)."""
        return self._observe_controller

    @property
    def viewer_controller(self) -> ViewerController:
        """The shared viewer controller (image-enhance ticket 01 — the
        Enhance view renders its snapshots)."""
        return self._viewer_controller

    @property
    def editor_controller(self) -> EditorController:
        """The shared editor controller (image-enhance ticket 03 — the
        Enhance view's sliders/buttons bind to it)."""
        return self._editor_controller

    @property
    def picker_controller(self) -> PickController:
        """The shared pick controller (photometry ticket 01 — the Photometry
        view renders its snapshots and binds its controls to it)."""
        return self._picker_controller

    @property
    def suggest_controller(self) -> SuggestionController:
        """The shared catalog-assist controller (photometry ticket 02 — the
        Photometry view's suggestions panel binds to it)."""
        return self._suggest_controller

    @property
    def run_controller(self) -> RunController:
        """The shared run + results controller (photometry ticket 04 — the
        Photometry view's results panel binds to it)."""
        return self._run_controller

    @property
    def lightcurve_controller(self) -> LightcurveController:
        """The shared lightcurve controller (period-analysis ticket 01 — the
        Lightcurve view binds to it; it follows the viewer controller's
        project)."""
        return self._lightcurve_controller

    @property
    def period_controller(self) -> PeriodController:
        """The shared period controller (period-analysis ticket 03 — the
        Lightcurve view's period panel binds to it; it is bound to the shared
        lightcurve controller)."""
        return self._period_controller

    @property
    def quality_controller(self) -> QualityController:
        """The shared quality controller (frame-quality ticket 02 — the
        Lightcurve view's session-quality section binds to it; it follows the
        viewer controller's project)."""
        return self._quality_controller

    @property
    def mover_controller(self) -> MoverController:
        """The shared mover controller (moving-objects ticket 02 — the
        Photometry view's mover overlay/blink widget binds to it; it follows
        the viewer controller's project)."""
        return self._mover_controller

    @property
    def crossmatch_controller(self) -> CrossmatchController:
        """The shared cross-match controller (gaia-crossmatch ticket 03 —
        the Enhance view's cross-match overlay panel binds to it; it follows
        the viewer controller's project and open stack)."""
        return self._crossmatch_controller

    @property
    def hr_diagram_controller(self) -> HrDiagramController:
        """The shared H-R diagram controller (hr-diagram ticket 03 — the
        H-R diagram view binds to it; it follows the viewer controller's
        project and open stack)."""
        return self._hr_diagram_controller

    @property
    def scan_controller(self) -> ScanController:
        """The shared variable-scan controller (variable-scan ticket 02 —
        the Photometry view's candidate panel binds to it; it follows the
        viewer controller's project)."""
        return self._scan_controller

    @property
    def solve_controller(self) -> SolveController:
        """The shared astrometry solve controller (astrometry ticket 03 —
        the Enhance view's solve panel binds to it; it follows the viewer
        controller's project and open stack)."""
        return self._solve_controller

    @property
    def drift_controller(self) -> DriftController:
        """The shared astrometry drift controller (astrometry ticket 03 —
        the Observe view's drift plot binds to it; it follows the viewer
        controller's project)."""
        return self._drift_controller

    @property
    def project_controller(self) -> ProjectController:
        """The shared project controller (projects-view ticket 01 — the
        Projects view lists/creates/opens through it; it is attached to the
        observe/viewer/picker/run/lightcurve/period controllers)."""
        return self._project_controller

    def _show_toast(self, title: str) -> None:
        """Surface a non-modal toast over the current view (astrometry ticket
        03 — solve/export outcomes). The overlay is created late in the
        constructor, so it is looked up lazily; before that (or in a window
        without one) the toast is dropped."""
        overlay = getattr(self, "_toast_overlay", None)
        if overlay is not None:
            overlay.add_toast(Adw.Toast(title=title, timeout=10))

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

    def _attach_project_followers(self) -> None:
        """Push the current project to every window controller with a
        ``set_project()`` (projects-view ticket 01), now and on every change."""
        self._project_controller.attach_controllers(
            observe=self._observe_controller,
            viewer=self._viewer_controller,
            picker=self._picker_controller,
            run=self._run_controller,
            lightcurve=self._lightcurve_controller,
            period=self._period_controller,
            quality=self._quality_controller,
            movers=self._mover_controller,
            crossmatch=self._crossmatch_controller,
            hr_diagram=self._hr_diagram_controller,
            scan=self._scan_controller,
        )

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
        """Observer: settings changed -> reflect the scope IP in the header
        and keep the photometry comp-search radius in sync (ticket 02)."""
        scope_ip = values.get("scope_ip", "")
        self._settings_button.set_tooltip_text(
            f"Settings (scope: {scope_ip})" if scope_ip else "Settings"
        )
        # comp_search_radius_deg() never raises (invalid -> default), and
        # set_radius() is a no-op when the radius is unchanged.
        self._suggest_controller.set_radius(comp_search_radius_deg(values))

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
        # Observing ticket 03: the shared observe controller over the
        # connection controller + queue controller + notification bus; the
        # window passes it to the Observe view (preview + progress +
        # night controls). Worker-thread callbacks are marshalled onto the
        # GLib main loop, like the connection controller's.
        self.observe_controller = ObserveController(
            self.connection_controller,
            self.queue_controller,
            self.notification_bus,
            invoke_on_main=_invoke_on_main,
        )
        # Image-enhance ticket 01: the shared viewer controller over the
        # current project's stacks; the window passes it to the Enhance
        # view. Starts with no project selected — the shared project
        # controller (projects-view ticket 01) calls set_project() once a
        # project is opened (see SeestarWindow).
        self.viewer_controller = ViewerController()
        # Image-enhance ticket 03: the shared editor controller over the
        # viewer controller (current pipeline + undo/redo + debounced
        # re-renders); the window passes it to the Enhance view. Worker-
        # thread render results are marshalled onto the GLib main loop,
        # like the connection controller's.
        self.editor_controller = EditorController(
            self.viewer_controller, invoke_on_main=_invoke_on_main
        )
        # Photometry ticket 01: the shared pick controller following the
        # shared viewer controller (the open stack becomes the pick
        # session's image); the window passes it to the Photometry view.
        self.picker_controller = PickController(viewer_controller=self.viewer_controller)
        # Photometry ticket 02: the shared catalog-assist controller, bound to
        # the shared pick controller and configured with the comp-search
        # radius from the settings; the window passes it to the Photometry
        # view's suggestions panel. The settings listener keeps the radius in
        # sync when the settings change (set_radius is a no-op when
        # unchanged).
        self.suggest_controller = SuggestionController(
            picker_controller=self.picker_controller,
            radius_deg=comp_search_radius_deg(self.settings_controller.values),
        )
        # Photometry ticket 04: the shared run + results controller, bound to
        # the shared pick controller (its picks are measured; the stored picks
        # are the fallback); the window passes it to the Photometry view's
        # results panel. Worker-thread runs marshal their store writes and
        # listener notifications onto the GLib main loop, like the connection
        # controller's.
        self.run_controller = RunController(
            picker_controller=self.picker_controller, invoke_on_main=_invoke_on_main
        )
        # Period-analysis ticket 01: the shared lightcurve controller following
        # the shared viewer controller (the open project becomes the
        # lightcurve's project); the window passes it to the Lightcurve view.
        # Store reads are marshalled onto the GLib main loop like the other
        # shared controllers'.
        self.lightcurve_controller = LightcurveController(
            viewer_controller=self.viewer_controller, invoke_on_main=_invoke_on_main
        )
        # Period-analysis ticket 03: the shared period controller bound to the
        # shared lightcurve controller (same project, same points); the window
        # passes it to the Lightcurve view's period panel. Store writes
        # (project.json persistence) and notifications are marshalled onto the
        # GLib main loop like the other shared controllers'.
        self.period_controller = PeriodController(
            self.lightcurve_controller, invoke_on_main=_invoke_on_main
        )
        # Frame-quality ticket 02: the shared quality controller following the
        # shared viewer controller (the open project becomes the quality
        # trends' project); the window passes it to the Lightcurve view's
        # session-quality section. Store reads and notifications are
        # marshalled onto the GLib main loop like the other shared
        # controllers'.
        self.quality_controller = QualityController(
            viewer_controller=self.viewer_controller, invoke_on_main=_invoke_on_main
        )
        # Moving-objects ticket 02: the shared mover controller following the
        # shared viewer controller (the open project becomes the mover
        # overlay's project); the window passes it to the Photometry view's
        # mover overlay/blink widget. Store reads/writes and notifications are
        # marshalled onto the GLib main loop like the other shared
        # controllers'.
        self.mover_controller = MoverController(
            viewer_controller=self.viewer_controller, invoke_on_main=_invoke_on_main
        )
        # Gaia-crossmatch ticket 03: the shared cross-match controller
        # following the shared viewer controller (the stack open in the image
        # viewer becomes the cross-match target); the window passes it to the
        # Enhance view's cross-match overlay panel. Store reads/writes and
        # notifications are marshalled onto the GLib main loop like the other
        # shared controllers'.
        self.crossmatch_controller = CrossmatchController(
            viewer_controller=self.viewer_controller, invoke_on_main=_invoke_on_main
        )
        # Hr-diagram ticket 03: the shared H-R diagram controller following
        # the shared viewer controller (the stack open in the image viewer
        # becomes the diagram target when it is crossmatched); the window
        # passes it to the H-R diagram view. Store reads/writes and
        # notifications are marshalled onto the GLib main loop like the other
        # shared controllers'.
        self.hr_diagram_controller = HrDiagramController(
            viewer_controller=self.viewer_controller, invoke_on_main=_invoke_on_main
        )
        # Variable-scan ticket 02: the shared scan controller following the
        # shared viewer controller (the open project becomes the candidate
        # view's project); the window passes it to the Photometry view's
        # variable-scan panel. Store reads/writes and notifications are
        # marshalled onto the GLib main loop like the other shared
        # controllers'.
        self.scan_controller = ScanController(
            viewer_controller=self.viewer_controller, invoke_on_main=_invoke_on_main
        )
        # Astrometry ticket 03: the shared solve + drift controllers following
        # the shared viewer controller (the stack open in the image viewer is
        # the solve/annotated-export target; the open project's sessions are
        # the drift-plot source); the window passes them to the Enhance
        # view's solve panel and the Observe view's drift plot. Store
        # reads/writes and notifications are marshalled onto the GLib main
        # loop like the other shared controllers'.
        self.solve_controller = SolveController(
            viewer_controller=self.viewer_controller, invoke_on_main=_invoke_on_main
        )
        self.drift_controller = DriftController(
            viewer_controller=self.viewer_controller, invoke_on_main=_invoke_on_main
        )
        # Projects-view ticket 01: the shared project controller over the same
        # state (project root from settings, last_project persisted). restore()
        # re-opens the last project when it still exists and clears a stale
        # last_project otherwise; attach_controllers() then pushes the current
        # project (restored or not) to every shared controller with a
        # set_project(), and keeps pushing it on every later change.
        # Ticket 02: an unset project_root falls back to the store default
        # (~/SeestarProjects, created on demand at create time); a changed
        # project_root re-lists the projects under the new root (see
        # _on_settings_changed). The attach list covers the observe / viewer /
        # picker / run / lightcurve / period / quality / movers / crossmatch /
        # hr_diagram controllers, so every current-project change is pushed to
        # all of them via their ``set_project``.
        self.project_controller = ProjectController(self._state)
        self.project_controller.restore()
        self.project_controller.attach_controllers(
            observe=self.observe_controller,
            viewer=self.viewer_controller,
            picker=self.picker_controller,
            run=self.run_controller,
            lightcurve=self.lightcurve_controller,
            period=self.period_controller,
            quality=self.quality_controller,
            movers=self.mover_controller,
            crossmatch=self.crossmatch_controller,
            hr_diagram=self.hr_diagram_controller,
            scan=self.scan_controller,
        )
        self.settings_controller.add_listener(self._on_settings_changed)
        self._last_project_root = self._state.get_setting("project_root")

    def _on_settings_changed(self, values: dict[str, str]) -> None:
        """Settings observer: keep the photometry comp-search radius (ticket
        02) in sync with the configured value, and re-list the projects when
        the project root changed (projects-view ticket 02 — e.g. the user
        fixed an unwritable root in the Settings dialog)."""
        self.suggest_controller.set_radius(comp_search_radius_deg(values))
        project_root = values.get("project_root", "")
        if project_root != self._last_project_root:
            self._last_project_root = project_root
            self.project_controller.refresh()

    @property
    def state(self) -> AppState:
        """The pure-layer app state (loaded from the settings file)."""
        return self._state

    def do_startup(self) -> None:
        # Chain up explicitly to the nearest parent vfunc: on PyGObject,
        # vfuncs reached through super() are not bound to the instance, so
        # super().do_startup() would call Gio.Application.startup() with no
        # argument and fail. Chaining to Adw.Application (not Gio) is
        # required so libadwaita's own startup (adw_init) runs.
        Adw.Application.do_startup(self)
        apply_dark_theme(self._state.dark_theme)

    def do_activate(self) -> None:
        window = SeestarWindow(self, self._state, self.settings_controller)
        window.present()


def run(argv: list[str] | None = None) -> int:
    """Run the GTK application; returns the exit status."""
    app = SeestarApplication()
    # GApplication.run treats extra positional arguments as files to open
    # ("This application can not open files" + exit 1), and the GUI takes
    # none — forward only the program name.
    args = sys.argv if argv is None else list(argv)
    return app.run(args[:1])


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m seestar_controller.ui.app``."""
    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
