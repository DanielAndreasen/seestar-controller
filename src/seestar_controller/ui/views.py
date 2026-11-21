"""View widgets for the ui shell — the WIDGET layer.

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by :mod:`seestar_controller.ui.state`
or eagerly by the package ``__init__`` — the pure layer stays gi-free and
headless-testable (see docs/wiki/assumptions.md: the sandbox has no GTK).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

The Lightcurve view (period-analysis tickets 01 + 03 + 04) carries the
:class:`~seestar_controller.analysis.lightcurve_view.LightcurveView` widget —
the mag-vs-JD plot of the current project's lightcurve (one color per
session + legend, error bars, excluded points marked, inverted mag axis,
zoom/pan, single-click on a point to toggle its outlier flag) — plus the
:class:`~seestar_controller.analysis.period_view.PeriodView` widget (ticket
03: periodogram plot with clickable peaks, manual period entry, phase-folded
lightcurve whose points are also clickable to toggle the outlier flag) and a
toolbar with the ticket-04 Export CSV / Export folded CSV buttons — driven by
the app's shared
:class:`~seestar_controller.analysis.lightcurve.LightcurveController` and
:class:`~seestar_controller.analysis.fold.PeriodController` (both following
the shared viewer controller's project; passed in via
:func:`build_view`). Below the period panel it also carries the
:class:`~seestar_controller.frame_quality.quality_view.SessionQualityView`
widget (frame-quality ticket 02: the per-session quality trends — FWHM /
background / airmass vs time plots + the summary card with the median FWHM and
the rejected percent) — driven by the app's shared
:class:`~seestar_controller.frame_quality.QualityController` (also following
the shared viewer controller's project; passed in via :func:`build_view`).
The **Projects** view (projects-view ticket 01) carries
the real content: the list of projects under the configured project root
(name / path / created-at, current project badged), a **New project** area
(name entry + Create button + inline errors), and an **Open** button per row
— driven by the app's shared
:class:`~seestar_controller.ui.project_controller.ProjectController` (passed
in via :func:`build_view`; a fresh one over a default
:class:`~seestar_controller.ui.state.AppState` is created when omitted, so
the view also constructs standalone). The
**Observe** view (observing tickets 01 + 03) already carries the real
content: the two-pane
:class:`~seestar_controller.observing.observe_view.ObserveView` widget — the
:class:`~seestar_controller.observing.queue_editor.QueueEditor` plus the
night controls on the left, the live preview + queue progress on the right —
driven by the app's shared
:class:`~seestar_controller.observing.observe_controller.ObserveController`
and
:class:`~seestar_controller.observing.queue_controller.PlanQueueController`
(both passed in by the app window via :func:`build_view`; fresh controllers
are created when omitted, so the view also constructs standalone). The
**Enhance** view (image-enhance tickets 01 + 03) carries the
:class:`~seestar_controller.enhance.enhance_view.EnhanceEditor` widget — the
project's stack list + per-op parameter sliders on the left, the live
before/after preview with undo/redo/reset on the right — driven by the app's
shared
:class:`~seestar_controller.enhance.viewer_controller.ViewerController` and
:class:`~seestar_controller.enhance.editor_controller.EditorController`
(both passed in via :func:`build_view`; with no project set yet the view
shows a "no project" placeholder — the Projects view is a later ticket). The
**Photometry** view (photometry tickets 01 + 02) carries the
:class:`~seestar_controller.photometry.star_picker.StarPickerView` widget —
the project's stack list + role/aperture controls + picks list + Save picks +
catalog-comps panel on the left, the clickable preview with the aperture
overlay on the right —
driven by the app's shared
:class:`~seestar_controller.photometry.picking.PickController` (which follows
the shared viewer controller's open stack), the shared
:class:`~seestar_controller.photometry.suggest.SuggestionController` (ticket
02, catalog assist for comps) and that same viewer controller (all passed in
via :func:`build_view`).
:data:`VIEW_CLASSES` maps the pure-layer view ids
(:mod:`seestar_controller.ui.state`) to the widget classes, and
:func:`build_view` is the factory the app window uses.

**Scrollable pages:** every page (:class:`_PlaceholderView`) wraps its content
in a vertical :class:`Gtk.ScrolledWindow` whose minimum height is only
:data:`PAGE_MIN_CONTENT_HEIGHT` — pages scroll instead of demanding their full
natural height, so the window's minimum size stays small enough for the WM to
apply maximize on laptop screens (see the class docstring).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk

from seestar_controller.analysis import (
    FOLDED_CSV_FILE,
    AnalysisError,
    ExportError,
    LightcurveController,
    PeriodController,
    export_folded_csv,
    export_lightcurve_csv,
)
from seestar_controller.analysis.lightcurve_view import (
    LightcurveView as LightcurveViewWidget,
)
from seestar_controller.analysis.period_view import (
    PeriodView as PeriodViewWidget,
)
from seestar_controller.enhance import EditorController, ViewerController
from seestar_controller.enhance.enhance_view import (
    EnhanceEditor as EnhanceEditorWidget,
)
from seestar_controller.frame_quality import QualityController
from seestar_controller.frame_quality.quality_view import (
    SessionQualityView as SessionQualityViewWidget,
)
from seestar_controller.observing import (
    ObserveController,
    PlanQueueController,
)
from seestar_controller.observing.observe_view import (
    ObserveView as ObserveViewWidget,
)
from seestar_controller.observing.queue_editor import QueueEditor
from seestar_controller.photometry import (
    PickController,
    RunController,
    SuggestionController,
)
from seestar_controller.photometry.results_view import (
    ResultsPanel as ResultsPanelWidget,
)
from seestar_controller.photometry.star_picker import (
    StarPickerView as StarPickerWidget,
)
from seestar_controller.store.csv_export import (
    CSV_FILE as LIGHTCURVE_CSV_FILE,
)
from seestar_controller.store.errors import ProjectError
from seestar_controller.visibility.timeline_view import (
    VisibilityView as VisibilityViewWidget,
)

from .connection import ConnectionController
from .project_controller import ProjectController, ProjectEvent
from .settings import SettingsController
from .state import VIEWS, AppState, view_by_id

#: Minimum content height (pixels) of a view page's scrolled window. Every
#: page scrolls vertically instead of demanding its full natural height, so
#: the window's program-specified minimum size stays small and the WM can
#: actually apply maximize on small screens (a page whose minimum exceeds the
#: workarea makes the WM drop the maximize request, leaving the window
#: smaller than the screen with controls off-screen).
PAGE_MIN_CONTENT_HEIGHT = 240


class _PlaceholderView(Gtk.Box):
    """Base placeholder view: a centered label naming the view.

    Subclasses set :attr:`view_id` / :attr:`title` (mirroring the pure-layer
    :class:`~seestar_controller.ui.state.View`) and may override
    :meth:`_build_content` to append real content instead of the placeholder
    label.

    Layout: the page is a thin outer box whose single child is a
    :class:`Gtk.ScrolledWindow` (vertical scrolling, never horizontal) over
    an inner content box (:attr:`content`) that :meth:`_build_content` fills.
    The scrolled window's minimum height is only
    :data:`PAGE_MIN_CONTENT_HEIGHT`, so the page — and therefore the
    :class:`Adw.ViewStack` and the window — never demands more than that;
    taller content stays reachable through the scrollbar.
    """

    view_id: str = ""
    title: str = ""

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("orientation", Gtk.Orientation.VERTICAL)
        kwargs.setdefault("spacing", 0)
        kwargs.setdefault("hexpand", True)
        kwargs.setdefault("vexpand", True)
        super().__init__(**kwargs)
        self._scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            min_content_height=PAGE_MIN_CONTENT_HEIGHT,
            hexpand=True,
            vexpand=True,
        )
        self._content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            hexpand=True,
            vexpand=True,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        self._scrolled.set_child(self._content)
        self.append(self._scrolled)
        self._build_content()

    @property
    def scrolled(self) -> Gtk.ScrolledWindow:
        """The page's vertical scrolled window (the page's only child)."""
        return self._scrolled

    @property
    def content(self) -> Gtk.Box:
        """The inner content box :meth:`_build_content` fills."""
        return self._content

    def _build_content(self) -> None:
        label = Gtk.Label(
            label=f"{self.title}\n(placeholder — this view is built by a later ticket)",
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        self._content.append(label)


class ObserveView(_PlaceholderView):
    """Observe view (observing tickets 01 + 03): wraps the two-pane
    :class:`~seestar_controller.observing.observe_view.ObserveView` widget —
    queue editor + night controls (start/pause/skip/stop/park) on the left,
    live preview + queue progress on the right.

    ``queue_controller`` and ``observe_controller`` are the app's shared
    controllers (the app window passes them via :func:`build_view`); when
    omitted, the view creates its own, so it also constructs standalone.
    ``project`` is the store project the Start button passes to
    ``start_night`` (the app passes the project controller's current project;
    it can also be set later via :meth:`observe_widget.set_project`).
    ``project_controller`` (projects-view ticket 01) is the app's shared
    :class:`~seestar_controller.ui.project_controller.ProjectController`;
    when given, the view listens to it and pushes every current-project
    change into the observe widget (so the Start button enables as soon as a
    project is open and the scope is connected).
    ``app_state`` (visibility-planner ticket 02) is the app's shared
    :class:`~seestar_controller.ui.state.AppState`; it feeds the night-timeline
    widget's site settings (a default ``AppState()`` is created when omitted,
    so the view also constructs standalone).
    """

    view_id = "observe"
    title = "Observe"

    def __init__(
        self,
        queue_controller: PlanQueueController | None = None,
        observe_controller: ObserveController | None = None,
        project: Any = None,
        project_controller: ProjectController | None = None,
        app_state: AppState | None = None,
        **kwargs,
    ) -> None:
        # set before super().__init__(): _build_content() runs inside it
        self._queue_controller = (
            queue_controller if queue_controller is not None else PlanQueueController()
        )
        self._observe_controller = (
            observe_controller
            if observe_controller is not None
            else ObserveController(
                ConnectionController(SettingsController(AppState())),
                self._queue_controller,
            )
        )
        self._project = project
        self._project_controller = project_controller
        self._app_state = app_state if app_state is not None else AppState()
        super().__init__(**kwargs)
        if self._project_controller is not None:
            self._project_controller.add_listener(self._on_project_event)

    def _on_project_event(self, event: ProjectEvent) -> None:
        """Project-controller listener: the current project changed -> push
        it into the observe widget (Start button enable + start_night)."""
        self._project = event.current
        self._observe_widget.set_project(event.current)

    def _build_content(self) -> None:
        self._observe_widget = ObserveViewWidget(
            self._observe_controller,
            queue_controller=self._queue_controller,
            project=self._project,
        )
        self._content.append(self._observe_widget)
        # visibility-planner ticket 02: the night timeline (altitude curves,
        # twilight, moon) + the suggested queue order with its Apply button,
        # below the observe widget on the same page. It reads the observing
        # site from the app state's live settings and the queue through the
        # shared queue controller's public API.
        self._visibility_widget = VisibilityViewWidget(
            self._queue_controller,
            settings=lambda: self._app_state.settings,
        )
        self._content.append(self._visibility_widget)

    @property
    def queue_controller(self) -> PlanQueueController:
        """The shared plan-queue controller backing this view."""
        return self._queue_controller

    @property
    def queue_editor(self) -> QueueEditor:
        """The queue editor widget (left pane of the observe widget)."""
        return self._observe_widget.queue_editor

    @property
    def observe_controller(self) -> ObserveController:
        """The shared pure-layer observe controller driving this view."""
        return self._observe_controller

    @property
    def observe_widget(self) -> ObserveViewWidget:
        """The two-pane observe widget (editor + controls | preview + progress)."""
        return self._observe_widget

    @property
    def project_controller(self) -> ProjectController | None:
        """The shared project controller this view follows (projects-view
        ticket 01), or ``None`` when constructed standalone."""
        return self._project_controller

    @property
    def app_state(self) -> AppState:
        """The app state feeding the night-timeline widget's site settings
        (visibility-planner ticket 02)."""
        return self._app_state

    @property
    def visibility_widget(self) -> VisibilityViewWidget:
        """The night-timeline widget (visibility-planner ticket 02)."""
        return self._visibility_widget


class EnhanceView(_PlaceholderView):
    """Enhance view (image-enhance tickets 01 + 03 + 04): wraps the
    :class:`~seestar_controller.enhance.enhance_view.EnhanceEditor` widget —
    the project's stack list + per-op parameter sliders on the left, the
    live before/after preview with undo/redo/reset on the right, plus the
    ticket-04 persistence buttons (Save enhanced / Save preset / Export PNG /
    Export JPEG, exposed as pass-through properties).

    ``viewer_controller`` is the app's shared
    :class:`~seestar_controller.enhance.viewer_controller.ViewerController`
    and ``editor_controller`` the shared
    :class:`~seestar_controller.enhance.editor_controller.EditorController`
    (the app window passes both via :func:`build_view`); when omitted, the
    view creates its own, so it also constructs standalone. With no project
    set on the viewer controller the view shows a "no project" placeholder
    (the Projects view is a later ticket; the project can be set later via
    :meth:`~seestar_controller.enhance.viewer_controller.ViewerController.set_project`).
    """

    view_id = "enhance"
    title = "Enhance"

    def __init__(
        self,
        viewer_controller: ViewerController | None = None,
        editor_controller: EditorController | None = None,
        **kwargs,
    ) -> None:
        # set before super().__init__(): _build_content() runs inside it
        self._viewer_controller = (
            viewer_controller if viewer_controller is not None else ViewerController()
        )
        self._editor_controller = (
            editor_controller
            if editor_controller is not None
            else EditorController(self._viewer_controller)
        )
        super().__init__(**kwargs)

    def _build_content(self) -> None:
        self._editor = EnhanceEditorWidget(
            self._viewer_controller, self._editor_controller
        )
        self._content.append(self._editor)

    @property
    def viewer_controller(self) -> ViewerController:
        """The shared pure-layer viewer controller backing this view."""
        return self._viewer_controller

    @property
    def editor_controller(self) -> EditorController:
        """The shared pure-layer editor controller driving this view."""
        return self._editor_controller

    @property
    def editor_widget(self) -> EnhanceEditorWidget:
        """The enhance editor widget (stack list + sliders | before/after preview)."""
        return self._editor

    @property
    def save_enhanced_button(self) -> Gtk.Button:
        """The widget's Save enhanced button (image-enhance ticket 04)."""
        return self._editor.save_enhanced_button

    @property
    def save_preset_button(self) -> Gtk.Button:
        """The widget's Save preset button (image-enhance ticket 04)."""
        return self._editor.save_preset_button

    @property
    def export_png_button(self) -> Gtk.Button:
        """The widget's Export PNG button (image-enhance ticket 04)."""
        return self._editor.export_png_button

    @property
    def export_jpeg_button(self) -> Gtk.Button:
        """The widget's Export JPEG button (image-enhance ticket 04)."""
        return self._editor.export_jpeg_button


class PhotometryView(_PlaceholderView):
    """Photometry view (photometry tickets 01 + 02 + 04): a vertical
    :class:`Gtk.Paned` with the star-picker widget on top and the results
    panel (ticket 04) below. Wraps the
    :class:`~seestar_controller.photometry.star_picker.StarPickerView` widget —
    the project's stack list + role selector + aperture spins + picks list +
    Save picks + catalog-comps panel on the left, the clickable preview with
    the aperture overlay on the right.

    ``picker_controller`` is the app's shared
    :class:`~seestar_controller.photometry.picking.PickController` (it follows
    the shared viewer controller's open stack), ``viewer_controller`` the
    shared
    :class:`~seestar_controller.enhance.viewer_controller.ViewerController`,
    and ``suggest_controller`` the shared catalog-assist
    :class:`~seestar_controller.photometry.suggest.SuggestionController`
    (ticket 02, bound to the pick controller and configured with the
    comp-search radius from the settings) — the app window passes all three
    via :func:`build_view`; when omitted, the view creates its own, so it
    also constructs standalone. With no project set on the viewer controller
    the view shows a "no stack open" placeholder (the Projects view is a
    later ticket; the project can be set later via
    :meth:`~seestar_controller.enhance.viewer_controller.ViewerController.set_project`).
    """

    view_id = "photometry"
    title = "Photometry"

    def __init__(
        self,
        picker_controller: PickController | None = None,
        viewer_controller: ViewerController | None = None,
        suggest_controller: SuggestionController | None = None,
        run_controller: RunController | None = None,
        **kwargs,
    ) -> None:
        # set before super().__init__(): _build_content() runs inside it
        if viewer_controller is None and picker_controller is not None:
            viewer_controller = picker_controller.viewer_controller
        self._viewer_controller = (
            viewer_controller if viewer_controller is not None else ViewerController()
        )
        self._picker_controller = (
            picker_controller
            if picker_controller is not None
            else PickController(viewer_controller=self._viewer_controller)
        )
        self._suggest_controller = suggest_controller
        self._run_controller = (
            run_controller
            if run_controller is not None
            else RunController(picker_controller=self._picker_controller)
        )
        super().__init__(**kwargs)

    def _build_content(self) -> None:
        self._picker_widget = StarPickerWidget(
            picker_controller=self._picker_controller,
            viewer_controller=self._viewer_controller,
            suggest_controller=self._suggest_controller,
        )
        self._results_panel = ResultsPanelWidget(self._run_controller)
        paned = Gtk.Paned(
            orientation=Gtk.Orientation.VERTICAL,
            start_child=self._picker_widget,
            end_child=self._results_panel,
            shrink_start_child=False,
            shrink_end_child=False,
            wide_handle=True,
        )
        self._content.append(paned)

    @property
    def picker_controller(self) -> PickController:
        """The shared pure-layer pick controller driving this view."""
        return self._picker_controller

    @property
    def viewer_controller(self) -> ViewerController:
        """The shared pure-layer viewer controller backing this view."""
        return self._viewer_controller

    @property
    def suggest_controller(self) -> SuggestionController:
        """The shared pure-layer catalog-assist controller (ticket 02) driving
        this view's suggestions panel."""
        return self._picker_widget.suggest_controller

    @property
    def run_controller(self) -> RunController:
        """The shared pure-layer run + results controller (ticket 04) driving
        this view's results panel."""
        return self._run_controller

    @property
    def results_panel(self) -> ResultsPanelWidget:
        """The results panel (session chooser + Run + table + scatter plot)."""
        return self._results_panel

    @property
    def picker_widget(self) -> StarPickerWidget:
        """The star-picker widget (stack list + controls | preview + overlay)."""
        return self._picker_widget

    @property
    def save_button(self) -> Gtk.Button:
        """The widget's Save picks button (photometry ticket 01)."""
        return self._picker_widget.save_button


class LightcurveView(_PlaceholderView):
    """Lightcurve view (period-analysis tickets 01 + 03 + 04, frame-quality
    ticket 02): wraps the
    :class:`~seestar_controller.analysis.lightcurve_view.LightcurveView`
    widget — the mag-vs-JD plot of the current project's lightcurve with one
    color per session + legend, error bars per point, excluded points marked,
    the inverted mag axis, zoom (scroll) / pan (drag) / double-click reset,
    and single-click on a data point to toggle its outlier flag (ticket 04)
    — and, below it, the
    :class:`~seestar_controller.analysis.period_view.PeriodView` widget
    (ticket 03): the periodogram plot with clickable top-N peaks, the manual
    period entry, and the phase-folded lightcurve (whose points are also
    clickable to toggle the outlier flag, ticket 04). A toolbar row on top
    carries the ticket-04 **Export CSV** / **Export folded CSV** buttons
    (async :class:`Gtk.FileDialog` save dialogs; the chosen path is
    marshalled back onto the main loop via the lightcurve controller's
    ``invoke_on_main``) plus a status label for export results. At the bottom
    the view carries the
    :class:`~seestar_controller.frame_quality.quality_view.SessionQualityView`
    widget (frame-quality ticket 02): the session selector + Refresh, the
    per-session summary card (median FWHM, rejected percent) and the three
    quality-trend plots (FWHM / background / airmass vs time).

    ``lightcurve_controller`` is the app's shared
    :class:`~seestar_controller.analysis.lightcurve.LightcurveController`
    (following the shared viewer controller's project — the currently open
    project) and ``period_controller`` the app's shared
    :class:`~seestar_controller.analysis.fold.PeriodController` bound to that
    same lightcurve controller (so both plots follow the open project), and
    ``quality_controller`` the app's shared
    :class:`~seestar_controller.frame_quality.QualityController` (frame-quality
    ticket 02, also following the shared viewer controller's project); the
    app window passes all three via :func:`build_view`. When omitted, the
    view creates its own, so it also constructs standalone.
    """

    view_id = "lightcurve"
    title = "Lightcurve"

    def __init__(
        self,
        lightcurve_controller: LightcurveController | None = None,
        period_controller: PeriodController | None = None,
        quality_controller: QualityController | None = None,
        **kwargs,
    ) -> None:
        # set before super().__init__(): _build_content() runs inside it
        self._lightcurve_controller = (
            lightcurve_controller
            if lightcurve_controller is not None
            else LightcurveController()
        )
        self._period_controller = (
            period_controller
            if period_controller is not None
            else PeriodController(self._lightcurve_controller)
        )
        self._quality_controller = (
            quality_controller
            if quality_controller is not None
            else QualityController()
        )
        super().__init__(**kwargs)

    def _build_content(self) -> None:
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_margin_top(6)
        toolbar.set_margin_bottom(0)
        toolbar.set_margin_start(6)
        toolbar.set_margin_end(6)
        self._export_csv_button = Gtk.Button(label="Export CSV")
        self._export_csv_button.connect("clicked", self._on_export_csv_clicked)
        toolbar.append(self._export_csv_button)
        self._export_folded_csv_button = Gtk.Button(label="Export folded CSV")
        self._export_folded_csv_button.connect("clicked", self._on_export_folded_csv_clicked)
        toolbar.append(self._export_folded_csv_button)
        self._export_status = Gtk.Label(label="", halign=Gtk.Align.START, hexpand=True)
        toolbar.append(self._export_status)
        self._content.append(toolbar)
        self._lightcurve_widget = LightcurveViewWidget(self._lightcurve_controller)
        self._content.append(self._lightcurve_widget)
        self._period_widget = PeriodViewWidget(self._period_controller)
        self._content.append(self._period_widget)
        # frame-quality ticket 02: the session-quality trends section
        self._quality_widget = SessionQualityViewWidget(self._quality_controller)
        self._content.append(self._quality_widget)

    # -- ticket 04: CSV export ------------------------------------------------------------

    def _set_export_status(self, message: str, *, error: bool) -> None:
        self._export_status.set_label(message)
        for css_class in ("error", "dim-label"):
            self._export_status.remove_css_class(css_class)
        self._export_status.add_css_class("error" if error else "dim-label")

    def _on_export_csv_clicked(self, *_args) -> None:
        """Open a :class:`Gtk.FileDialog` save dialog for the lightcurve CSV;
        the async callback marshals the chosen path back onto the main loop
        via the lightcurve controller's ``invoke_on_main``."""
        self._open_export_dialog("Export lightcurve CSV", LIGHTCURVE_CSV_FILE, "lightcurve")

    def _on_export_folded_csv_clicked(self, *_args) -> None:
        """Same as :meth:`_on_export_csv_clicked` for the folded CSV."""
        self._open_export_dialog("Export folded CSV", FOLDED_CSV_FILE, "folded")

    def _open_export_dialog(self, title: str, initial_name: str, kind: str) -> None:
        dialog = Gtk.FileDialog(title=title, accept_label=title)
        dialog.set_initial_name(initial_name)
        dialog.save(
            self.get_root(),
            None,
            lambda dlg, result, kind=kind: self._on_export_dialog_finished(dlg, result, kind),
        )

    def _on_export_dialog_finished(self, dialog: Gtk.FileDialog, result, kind: str) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return  # cancelled
        if file is None:
            return
        path = file.get_path()
        if not path:
            return
        # the dialog callback is NOT on the main loop's controller context —
        # marshal the export through invoke_on_main (GLib.idle_add in the app)
        self._lightcurve_controller.invoke_on_main(lambda: self._do_export(path, kind))

    def _do_export(self, path: str, kind: str) -> None:
        """Run the pure export on the main loop; ``kind`` is ``"lightcurve"``
        or ``"folded"``. Errors surface as a red status line."""
        project = self._lightcurve_controller.project
        if project is None:
            self._set_export_status("no project open — nothing to export", error=True)
            return
        try:
            if kind == "lightcurve":
                written = export_lightcurve_csv(project, path=path)
            elif kind == "folded":
                period = self._period_controller.period
                if period is None:
                    self._set_export_status(
                        "no period selected — compute the periodogram or set a period first",
                        error=True,
                    )
                    return
                jd0 = self._period_controller.snapshot.jd0
                written = export_folded_csv(project, period, path=path, jd0=jd0)
            else:
                raise ExportError(f"unknown export kind: {kind!r}")
        except (AnalysisError, OSError) as exc:
            self._set_export_status(f"export failed: {exc}", error=True)
            return
        self._set_export_status(f"exported {written}", error=False)

    def export_lightcurve_to(self, path: str) -> None:
        """Test/programmatic helper: export the lightcurve CSV to ``path``
        (same path as the Export CSV button, without the file dialog)."""
        self._do_export(path, "lightcurve")

    def export_folded_to(self, path: str) -> None:
        """Test/programmatic helper: export the folded CSV to ``path`` (same
        path as the Export folded CSV button, without the file dialog)."""
        self._do_export(path, "folded")

    # -- properties ------------------------------------------------------------------------

    @property
    def lightcurve_controller(self) -> LightcurveController:
        """The shared pure-layer lightcurve controller driving this view."""
        return self._lightcurve_controller

    @property
    def period_controller(self) -> PeriodController:
        """The shared pure-layer period controller (ticket 03) driving the
        period-analysis panel of this view."""
        return self._period_controller

    @property
    def lightcurve_widget(self) -> LightcurveViewWidget:
        """The lightcurve plot widget (mag vs JD, zoom/pan, click-to-flag)."""
        return self._lightcurve_widget

    @property
    def period_widget(self) -> PeriodViewWidget:
        """The period-analysis widget (periodogram + manual entry + folded
        plot, tickets 03 + 04)."""
        return self._period_widget

    @property
    def quality_controller(self) -> QualityController:
        """The shared pure-layer quality controller (frame-quality ticket 02)
        driving the session-quality section of this view."""
        return self._quality_controller

    @property
    def quality_widget(self) -> SessionQualityViewWidget:
        """The session-quality widget (summary card + FWHM / background /
        airmass trend plots, frame-quality ticket 02)."""
        return self._quality_widget

    @property
    def export_csv_button(self) -> Gtk.Button:
        """The Export CSV button (period-analysis ticket 04)."""
        return self._export_csv_button

    @property
    def export_folded_csv_button(self) -> Gtk.Button:
        """The Export folded CSV button (period-analysis ticket 04)."""
        return self._export_folded_csv_button


class ProjectsView(_PlaceholderView):
    """Projects view (projects-view ticket 01): list / create / open the
    projects under the configured project root, wired as the app's current
    project.

    A thin observer over a pure
    :class:`~seestar_controller.ui.project_controller.ProjectController`
    (the app passes its shared instance via :func:`build_view`; a fresh one
    over a default :class:`~seestar_controller.ui.state.AppState` is created
    when omitted, so the view also constructs standalone):

    - a **current-project banner** on top (name + path, or a hint that no
      project is open) and a dim **project-root** line (pointing to Settings
      when the root is not configured and the default is in use, and shown in
      red with the Settings hint when the root exists but is unusable — e.g.
      not writable, projects-view ticket 02);
    - a :class:`Gtk.ListBox` of the projects (name, path, created-at from the
      :class:`~seestar_controller.store.ProjectSummary`), one row each with
      an **Open** button; the current project's row carries a "current"
      badge;
    - a **New project** area: a name :class:`Adw.EntryRow` + a **Create
      project** button (creates via the controller and opens the new project
      — it becomes current; a missing project root is created on demand) +
      an inline status label for errors (duplicate / invalid name /
      unwritable root — the root errors carry a hint pointing to Settings),
      the same pattern as
      :class:`~seestar_controller.observing.queue_editor.QueueEditor`.

    The view re-renders on every controller event (it registers as a
    listener), so changes from anywhere show up.
    """

    view_id = "projects"
    title = "Projects"

    def __init__(
        self, project_controller: ProjectController | None = None, **kwargs
    ) -> None:
        # set before super().__init__(): _build_content() runs inside it
        self._project_controller = (
            project_controller
            if project_controller is not None
            else ProjectController(AppState())
        )
        #: per-row Open buttons, rebuilt with the list
        self._row_buttons: list[Gtk.Button] = []
        #: per-row "current" badges, rebuilt with the list
        self._row_badges: list[Gtk.Label] = []
        super().__init__(**kwargs)

    def _build_content(self) -> None:
        # current-project banner + project-root line
        self._current_label = Gtk.Label(
            label="", wrap=True, halign=Gtk.Align.START, xalign=0.0
        )
        self._content.append(self._current_label)
        self._root_label = Gtk.Label(
            label="",
            wrap=True,
            halign=Gtk.Align.START,
            xalign=0.0,
            css_classes=["dim-label"],
        )
        self._content.append(self._root_label)

        # the project list
        self._list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        scrolled = Gtk.ScrolledWindow(
            child=self._list_box,
            vexpand=True,
            min_content_height=160,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        self._content.append(scrolled)

        # new-project area: name entry + Create button + inline status
        group = Adw.PreferencesGroup(title="New project")
        self._name_entry = Adw.EntryRow(title="Project name (e.g. 'M 42')")
        group.add(self._name_entry)
        self._content.append(group)
        buttons = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.CENTER
        )
        self._create_button = Gtk.Button(
            label="Create project",
            css_classes=["suggested-action"],
            tooltip_text="Create a new project under the project root and open it",
        )
        self._create_button.connect("clicked", self._on_create_clicked)
        buttons.append(self._create_button)
        self._content.append(buttons)
        self._status_label = Gtk.Label(label="", wrap=True, halign=Gtk.Align.CENTER)
        self._content.append(self._status_label)

        self._project_controller.add_listener(self._on_project_event)
        self._rebuild()

    # -- public helpers (also used by the widget tests) ---------------------------

    @property
    def controller(self) -> ProjectController:
        """The pure-layer project controller backing this view."""
        return self._project_controller

    @property
    def list_box(self) -> Gtk.ListBox:
        """The project list."""
        return self._list_box

    @property
    def current_label(self) -> Gtk.Label:
        """The current-project banner label."""
        return self._current_label

    @property
    def root_label(self) -> Gtk.Label:
        """The project-root line (dim label)."""
        return self._root_label

    @property
    def name_entry(self) -> Adw.EntryRow:
        """The new-project name entry."""
        return self._name_entry

    @property
    def create_button(self) -> Gtk.Button:
        """The Create project button."""
        return self._create_button

    @property
    def status_label(self) -> Gtk.Label:
        """The inline status label (errors in red, success in green)."""
        return self._status_label

    def row_count(self) -> int:
        """The number of project rows currently rendered."""
        return len(self._row_buttons)

    def row_label(self, index: int) -> str:
        """The main label text of the row at ``index`` (the project name)."""
        row = self._list_box.get_row_at_index(index)
        assert row is not None
        box = row.get_child()
        labels = box.get_first_child()  # the vertical label box
        return labels.get_first_child().get_label()

    def row_subtitle(self, index: int) -> str:
        """The subtitle of the row at ``index`` (path + created-at)."""
        row = self._list_box.get_row_at_index(index)
        assert row is not None
        box = row.get_child()
        labels = box.get_first_child()
        return labels.get_last_child().get_label()

    def is_current_row(self, index: int) -> bool:
        """Whether the row at ``index`` carries the "current" badge."""
        return self._row_badges[index].get_visible()

    def click_open(self, index: int) -> None:
        """Click the Open button of the row at ``index``."""
        self._row_buttons[index].emit("clicked")

    # -- rendering ------------------------------------------------------------------

    def _on_project_event(self, event: ProjectEvent) -> None:
        """Controller listener: projects and/or current changed -> re-render."""
        self._rebuild()

    def _rebuild(self) -> None:
        """Re-render the banner, root line, and project list from the
        controller."""
        while (row := self._list_box.get_first_child()) is not None:
            self._list_box.remove(row)
        self._row_buttons = []
        self._row_badges = []
        current = self._project_controller.current
        for summary in self._project_controller.projects:
            self._list_box.append(self._make_row(summary, current))
        if current is not None:
            self._current_label.set_label(
                f"Current project: {current.name} ({current.path})"
            )
            self._current_label.remove_css_class("dim-label")
        else:
            self._current_label.set_label(
                "No project open — create or open one below; "
                "new data is saved into the current project."
            )
            self._current_label.add_css_class("dim-label")
        self._render_root_label()

    def _render_root_label(self) -> None:
        """Render the project-root line: the root in use, the "not set —
        using the default" hint (pointing to Settings) when the setting is
        unset, or a red error line (with the Settings hint) when the root
        exists but is unusable (projects-view ticket 02)."""
        root = self._project_controller.root
        root_error = self._project_controller.root_error
        for css_class in ("error", "dim-label"):
            self._root_label.remove_css_class(css_class)
        if root_error is not None:
            self._root_label.set_label(f"Project root problem: {root_error}")
            self._root_label.add_css_class("error")
        elif self._project_controller.state.get_setting("project_root").strip():
            self._root_label.set_label(f"Project root: {root}")
            self._root_label.add_css_class("dim-label")
        else:
            self._root_label.set_label(
                f"Project root not set — using the default {root} "
                "(created on demand; set a project root in Settings)"
            )
            self._root_label.add_css_class("dim-label")

    def _make_row(self, summary, current: Any) -> Gtk.ListBoxRow:
        box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin_top=6,
            margin_bottom=6,
            margin_start=12,
            margin_end=12,
        )
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        name_label = Gtk.Label(
            label=summary.name, xalign=0.0, valign=Gtk.Align.CENTER
        )
        subtitle_label = Gtk.Label(
            label=f"{summary.path} · created {summary.created_at}",
            xalign=0.0,
            valign=Gtk.Align.CENTER,
            css_classes=["dim-label"],
            ellipsize=3,  # Pango.EllipsizeMode.END
        )
        labels.append(name_label)
        labels.append(subtitle_label)
        box.append(labels)
        is_current = current is not None and Path(current.path) == summary.path
        badge = Gtk.Label(
            label="current",
            valign=Gtk.Align.CENTER,
            visible=is_current,
            css_classes=["success"],
        )
        box.append(badge)
        open_button = Gtk.Button(
            label="Open",
            valign=Gtk.Align.CENTER,
            tooltip_text=f"Open {summary.name} as the current project",
        )
        open_button.connect("clicked", lambda _btn, path=summary.path: self._on_open_clicked(path))
        box.append(open_button)
        self._row_buttons.append(open_button)
        self._row_badges.append(badge)
        return Gtk.ListBoxRow(child=box, activatable=False)

    # -- actions ----------------------------------------------------------------------

    def _set_status(self, text: str, *, error: bool) -> None:
        self._status_label.set_label(text)
        for css_class in ("error", "success", "dim-label"):
            self._status_label.remove_css_class(css_class)
        if error:
            self._status_label.add_css_class("error")
        elif text:
            self._status_label.add_css_class("success")
        else:
            self._status_label.add_css_class("dim-label")

    def _on_create_clicked(self, *_args) -> None:
        """Create button: create + open through the controller; inline error
        on failure (duplicate/invalid name, unwritable root — the root errors
        carry the Settings hint)."""
        name = self._name_entry.get_text().strip()
        try:
            project = self._project_controller.create_project(name)
        except (ProjectError, ValueError, OSError) as exc:
            # re-render the root line too: an unusable root shows there as well
            self._render_root_label()
            self._set_status(str(exc), error=True)
            return
        self._name_entry.set_text("")
        self._set_status(
            f"created and opened {project.name} ({project.path})", error=False
        )

    def _on_open_clicked(self, path) -> None:
        """Open button: open the project at ``path`` through the controller;
        inline error on failure (missing/invalid project)."""
        try:
            project = self._project_controller.open_project(path)
        except (ProjectError, ValueError, OSError) as exc:
            self._set_status(str(exc), error=True)
            return
        self._set_status(f"opened {project.name} ({project.path})", error=False)


#: view id -> widget class (one entry per pure-layer view).
VIEW_CLASSES: dict[str, type[_PlaceholderView]] = {
    view.id: cls
    for view, cls in (
        (VIEWS[0], ObserveView),
        (VIEWS[1], EnhanceView),
        (VIEWS[2], PhotometryView),
        (VIEWS[3], LightcurveView),
        (VIEWS[4], ProjectsView),
    )
}


def build_view(
    view_id: str,
    *,
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
    project_controller: ProjectController | None = None,
    project: Any = None,
    app_state: AppState | None = None,
) -> _PlaceholderView:
    """Construct the widget for ``view_id``.

    Raises :class:`~seestar_controller.ui.state.UnknownViewError` for an id
    not in the pure-layer view list. ``queue_controller`` /
    ``observe_controller`` / ``project_controller`` / ``project`` /
    ``app_state`` are forwarded to the Observe view (observing tickets 01 +
    03, projects-view ticket 01, visibility-planner ticket 02),
    ``viewer_controller`` /
    ``editor_controller`` to the Enhance view (image-enhance tickets 01 + 03),
    ``picker_controller`` / ``viewer_controller`` / ``suggest_controller``
    / ``run_controller`` to the Photometry view (photometry tickets 01 + 02 +
    04), ``lightcurve_controller`` / ``period_controller`` /
    ``quality_controller`` to the Lightcurve view (period-analysis tickets
    01 + 03 + 04 and frame-quality ticket 02), and
    ``project_controller`` to the Projects view (projects-view ticket 01) —
    the app window passes its shared controllers; the other views ignore
    them.
    """
    view_by_id(view_id)  # validate (raises UnknownViewError)
    if view_id == ObserveView.view_id:
        return ObserveView(
            queue_controller=queue_controller,
            observe_controller=observe_controller,
            project_controller=project_controller,
            project=project,
            app_state=app_state,
        )
    if view_id == EnhanceView.view_id:
        return EnhanceView(
            viewer_controller=viewer_controller,
            editor_controller=editor_controller,
        )
    if view_id == PhotometryView.view_id:
        return PhotometryView(
            picker_controller=picker_controller,
            viewer_controller=viewer_controller,
            suggest_controller=suggest_controller,
            run_controller=run_controller,
        )
    if view_id == LightcurveView.view_id:
        return LightcurveView(
            lightcurve_controller=lightcurve_controller,
            period_controller=period_controller,
            quality_controller=quality_controller,
        )
    if view_id == ProjectsView.view_id:
        return ProjectsView(project_controller=project_controller)
    return VIEW_CLASSES[view_id]()
