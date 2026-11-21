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
the view also constructs standalone). Offline-analysis ticket 02 adds the
**stored-data browser** to the Projects view: below the project list, one row
per session of the current project (id, open/closed, per-kind frame counts,
plus **Stitch mosaic**, **Stack subs**, and **Session report** action buttons)
followed by one row per stored frame (kind, id, jd, exposure, target) — all
read from the project store, so the whole browser works in offline mode with
no scope connection; the actions run on the main loop (via ``invoke_on_main``,
like every other store call) and write their products back into the project
(``mosaics/<session>.fits`` / ``reports/<session>.html``). The
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

from collections.abc import Callable
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
from seestar_controller.astrometry import DriftController, SolveController
from seestar_controller.astrometry.view import (
    DriftPlotView as DriftPlotWidget,
)
from seestar_controller.astrometry.view import (
    SolvePanel as SolvePanelWidget,
)
from seestar_controller.enhance import EditorController, ViewerController
from seestar_controller.enhance.enhance_view import (
    EnhanceEditor as EnhanceEditorWidget,
)
from seestar_controller.frame_quality import QualityController
from seestar_controller.frame_quality.quality_view import (
    SessionQualityView as SessionQualityViewWidget,
)
from seestar_controller.gaia import CrossmatchController
from seestar_controller.gaia.overlay_view import (
    CrossmatchOverlayView as CrossmatchOverlayWidget,
)
from seestar_controller.hr_diagram import HrDiagramController
from seestar_controller.hr_diagram.view import (
    HrDiagramView as HrDiagramViewWidget,
)
from seestar_controller.mosaic import mosaic_path, stitch_session
from seestar_controller.moving_objects import MoverController
from seestar_controller.moving_objects.overlay_view import (
    MoverOverlayView as MoverOverlayWidget,
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
from seestar_controller.report import generate_report, report_path
from seestar_controller.stacking import stack_session_subs
from seestar_controller.store import KINDS, list_frames, list_sessions
from seestar_controller.store.csv_export import (
    CSV_FILE as LIGHTCURVE_CSV_FILE,
)
from seestar_controller.store.errors import ProjectError
from seestar_controller.variable_scan import ScanController
from seestar_controller.variable_scan.view import (
    VariableScanView as VariableScanWidget,
)
from seestar_controller.visibility.timeline_view import (
    VisibilityView as VisibilityViewWidget,
)

from .connection import ConnectionController
from .mode import OFFLINE_PREVIEW_LABEL, OFFLINE_SCOPE_TOOLTIP
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


def _direct_invoke(callback: Callable[[], None]) -> None:
    """The default ``invoke_on_main`` for standalone views: run the callback
    directly (the app window binds ``GLib.idle_add`` instead)."""
    callback()


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
    ``drift_controller`` (astrometry ticket 03) is the app's shared
    :class:`~seestar_controller.astrometry.DriftController`; when omitted, the
    view creates its own, so it also constructs standalone. The drift plot
    widget sits below the night timeline on the same page.

    Offline mode (offline-analysis ticket 01): :meth:`set_scope_gated` gates
    the scope-dependent controls — the night-control buttons (start /
    pause / skip / stop / park, where start performs the goto/slew) become
    insensitive with the offline tooltip and the live preview is hidden while
    gated; the gating is re-applied after every observe-controller state
    change, so the buttons stay disabled for the whole offline session.
    """

    view_id = "observe"
    title = "Observe"

    def __init__(
        self,
        queue_controller: PlanQueueController | None = None,
        observe_controller: ObserveController | None = None,
        drift_controller: DriftController | None = None,
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
        self._drift_controller = (
            drift_controller if drift_controller is not None else DriftController()
        )
        self._project = project
        self._project_controller = project_controller
        self._app_state = app_state if app_state is not None else AppState()
        # offline-analysis ticket 01: scope gating (offline mode) — the
        # night-control buttons + live preview are disabled/hidden while
        # gated; the base sensitivity/tooltip are remembered to restore them
        self._scope_gated = False
        self._gated_base: dict[Gtk.Button, bool] = {}
        self._gated_tooltips: dict[Gtk.Button, str | None] = {}
        self._gated_preview_hidden = False
        self._gated_preview_visible = True
        self._gated_preview_label = ""
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
        # offline-analysis ticket 01: re-apply the scope gating after every
        # observe-controller state change (this listener is registered after
        # the widget's own, so it runs after the widget re-rendered the
        # buttons' sensitivity)
        self._observe_controller.add_listener(self._on_observe_state_gating)
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
        # astrometry ticket 03: the drift plot (RA/Dec offset vs time of the
        # session's solved frames) below the night timeline, driven by the
        # shared drift controller (which follows the shared viewer
        # controller's project).
        self._drift_plot = DriftPlotWidget(drift_controller=self._drift_controller)
        self._content.append(self._drift_plot)

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

    @property
    def drift_controller(self) -> DriftController:
        """The shared drift controller (astrometry ticket 03) driving this
        view's drift plot."""
        return self._drift_controller

    @property
    def drift_plot(self) -> DriftPlotWidget:
        """The drift plot widget (astrometry ticket 03)."""
        return self._drift_plot

    # -- offline-mode scope gating (offline-analysis ticket 01) -------------------

    @property
    def scope_gated(self) -> bool:
        """Whether the scope-dependent controls are gated (offline mode)."""
        return self._scope_gated

    def _scope_buttons(self) -> tuple[Gtk.Button, ...]:
        """The scope-dependent night-control buttons (start/pause/skip/stop/
        park — start performs the goto/slew to the target)."""
        widget = self._observe_widget
        return (
            widget.start_button,
            widget.pause_button,
            widget.skip_button,
            widget.stop_button,
            widget.park_button,
        )

    def set_scope_gated(self, gated: bool) -> None:
        """Gate/ungate the scope-dependent controls (offline-analysis ticket
        01): while gated (offline mode) the night-control buttons are
        insensitive with the offline tooltip and the live preview is hidden;
        ungating restores exactly the widgets gating changed (the remembered
        base sensitivity/tooltips and the preview) and leaves untouched
        widgets alone, so the observe widget's own state-driven rendering is
        never clobbered."""
        self._scope_gated = bool(gated)
        self._apply_scope_gating()

    def _on_observe_state_gating(self, _state) -> None:
        """Observe-controller listener: re-apply the gating after the widget
        re-rendered (only while gated — ungated leaves the widget alone)."""
        if self._scope_gated:
            self._apply_scope_gating()

    def _apply_scope_gating(self) -> None:
        """Apply the current gating to the night-control buttons + preview.

        While gating, the base sensitivity/tooltip (and the preview's
        visibility/label) are remembered exactly once — re-applying the gate
        after an observe-state change must not overwrite the remembered base
        with the gated values. While ungating, only widgets present in the
        remembered maps are restored; untouched widgets keep whatever the
        observe widget's own state-driven rendering last set."""
        widget = self._observe_widget
        for button in self._scope_buttons():
            if self._scope_gated:
                if button not in self._gated_base:
                    self._gated_base[button] = button.get_sensitive()
                if button not in self._gated_tooltips:
                    self._gated_tooltips[button] = button.get_tooltip_text()
                button.set_sensitive(False)
                button.set_tooltip_text(OFFLINE_SCOPE_TOOLTIP)
            elif button in self._gated_base:
                button.set_sensitive(self._gated_base.pop(button))
                if button in self._gated_tooltips:
                    button.set_tooltip_text(self._gated_tooltips.pop(button))
        if self._scope_gated:
            if not self._gated_preview_hidden:
                self._gated_preview_hidden = True
                self._gated_preview_visible = widget.picture.get_visible()
                self._gated_preview_label = widget.preview_label.get_label()
            widget.picture.set_visible(False)
            widget.preview_label.set_label(OFFLINE_PREVIEW_LABEL)
        elif self._gated_preview_hidden:
            self._gated_preview_hidden = False
            widget.picture.set_visible(self._gated_preview_visible)
            widget.preview_label.set_label(self._gated_preview_label)


class EnhanceView(_PlaceholderView):
    """Enhance view (image-enhance tickets 01 + 03 + 04, gaia-crossmatch
    ticket 03): wraps the
    :class:`~seestar_controller.enhance.enhance_view.EnhanceEditor` widget —
    the project's stack list + per-op parameter sliders on the left, the
    live before/after preview with undo/redo/reset on the right, plus the
    ticket-04 persistence buttons (Save enhanced / Save preset / Export PNG /
    Export JPEG, exposed as pass-through properties) — and below it the
    gaia-crossmatch ticket-03 panel
    (:class:`~seestar_controller.gaia.overlay_view.CrossmatchOverlayView`),
    the cross-match overlay of the stack open in the viewer.

    ``viewer_controller`` is the app's shared
    :class:`~seestar_controller.enhance.viewer_controller.ViewerController`
    and ``editor_controller`` the shared
    :class:`~seestar_controller.enhance.editor_controller.EditorController`
    (the app window passes both via :func:`build_view`); when omitted, the
    view creates its own, so it also constructs standalone.
    ``crossmatch_controller`` is the app's shared
    :class:`~seestar_controller.gaia.overlay.CrossmatchController` (the
    gaia-crossmatch ticket 03) driving the cross-match overlay panel; when
    omitted, the view creates its own. ``solve_controller`` is the app's
    shared :class:`~seestar_controller.astrometry.SolveController` (astrometry
    ticket 03) driving the solve panel below the overlay (Solve + Export
    annotated PNG of the stack open in the viewer); when omitted, the view
    creates its own following its viewer controller. ``show_toast`` is an
    optional ``show_toast(title)`` callable the solve panel reports its
    outcomes through (the window passes its toast overlay). With no project
    set on the viewer
    controller the view shows a "no project" placeholder (the Projects view
    is a later ticket; the project can be set later via
    :meth:`~seestar_controller.enhance.viewer_controller.ViewerController.set_project`).
    """

    view_id = "enhance"
    title = "Enhance"

    def __init__(
        self,
        viewer_controller: ViewerController | None = None,
        editor_controller: EditorController | None = None,
        crossmatch_controller: CrossmatchController | None = None,
        solve_controller: SolveController | None = None,
        show_toast: Any = None,
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
        self._crossmatch_controller = (
            crossmatch_controller
            if crossmatch_controller is not None
            else CrossmatchController(viewer_controller=self._viewer_controller)
        )
        self._solve_controller = (
            solve_controller
            if solve_controller is not None
            else SolveController(viewer_controller=self._viewer_controller)
        )
        self._show_toast = show_toast
        super().__init__(**kwargs)

    def _build_content(self) -> None:
        self._editor = EnhanceEditorWidget(
            self._viewer_controller, self._editor_controller
        )
        self._content.append(self._editor)
        # gaia-crossmatch ticket 03: the cross-match overlay panel below the
        # image editor (the cross-match panel of the image viewer)
        self._crossmatch_overlay = CrossmatchOverlayWidget(
            crossmatch_controller=self._crossmatch_controller
        )
        self._content.append(self._crossmatch_overlay)
        # astrometry ticket 03: the solve panel (Solve + Export annotated
        # PNG of the stack open in the viewer) below the cross-match overlay
        self._solve_panel = SolvePanelWidget(
            solve_controller=self._solve_controller,
            show_toast=self._show_toast,
        )
        self._content.append(self._solve_panel)

    @property
    def viewer_controller(self) -> ViewerController:
        """The shared pure-layer viewer controller backing this view."""
        return self._viewer_controller

    @property
    def editor_controller(self) -> EditorController:
        """The shared pure-layer editor controller driving this view."""
        return self._editor_controller

    @property
    def crossmatch_controller(self) -> CrossmatchController:
        """The shared cross-match controller (gaia-crossmatch ticket 03)
        driving this view's cross-match overlay panel."""
        return self._crossmatch_controller

    @property
    def crossmatch_overlay(self) -> CrossmatchOverlayWidget:
        """The cross-match overlay panel (gaia-crossmatch ticket 03)."""
        return self._crossmatch_overlay

    @property
    def solve_controller(self) -> SolveController:
        """The shared solve controller (astrometry ticket 03) driving this
        view's solve panel."""
        return self._solve_controller

    @property
    def solve_panel(self) -> SolvePanelWidget:
        """The solve panel (astrometry ticket 03 — Solve + Export annotated
        PNG of the stack open in the image viewer)."""
        return self._solve_panel

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
    the aperture overlay on the right. Below the paned it also carries the
    moving-objects ticket-02
    :class:`~seestar_controller.moving_objects.overlay_view.MoverOverlayView`
    widget — the annotated overlay/blink view of the session's movers — and
    the variable-scan ticket-02
    :class:`~seestar_controller.variable_scan.view.VariableScanView` widget —
    the ranked variable-candidate list with the selected candidate's
    lightcurve + blink.

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
    ``mover_controller`` is the app's shared
    :class:`~seestar_controller.moving_objects.overlay.MoverController`
    (moving-objects ticket 02) driving the mover overlay/blink widget — also
    passed in via :func:`build_view`, also defaulted when omitted.
    ``scan_controller`` is the app's shared
    :class:`~seestar_controller.variable_scan.controller.ScanController`
    (variable-scan ticket 02) driving the variable-candidate widget — also
    passed in via :func:`build_view`, also defaulted when omitted.
    """

    view_id = "photometry"
    title = "Photometry"

    def __init__(
        self,
        picker_controller: PickController | None = None,
        viewer_controller: ViewerController | None = None,
        suggest_controller: SuggestionController | None = None,
        run_controller: RunController | None = None,
        mover_controller: MoverController | None = None,
        scan_controller: ScanController | None = None,
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
        self._mover_controller = (
            mover_controller if mover_controller is not None else MoverController()
        )
        self._scan_controller = (
            scan_controller if scan_controller is not None else ScanController()
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
        # moving-objects ticket 02: the annotated overlay/blink view of the
        # session's movers below the photometry results panel
        self._mover_overlay = MoverOverlayWidget(mover_controller=self._mover_controller)
        self._content.append(self._mover_overlay)
        # variable-scan ticket 02: the ranked variable-candidate list with the
        # selected candidate's lightcurve + blink, next to the mover overlay
        self._variable_scan = VariableScanWidget(scan_controller=self._scan_controller)
        self._content.append(self._variable_scan)

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
    def mover_controller(self) -> MoverController:
        """The shared pure-layer moving-objects controller (moving-objects
        ticket 02) driving this view's mover overlay/blink widget."""
        return self._mover_controller

    @property
    def mover_overlay(self) -> MoverOverlayWidget:
        """The mover overlay/blink widget (moving-objects ticket 02)."""
        return self._mover_overlay

    @property
    def scan_controller(self) -> ScanController:
        """The shared pure-layer variable-scan controller (variable-scan
        ticket 02) driving this view's variable-candidate widget."""
        return self._scan_controller

    @property
    def variable_scan_view(self) -> VariableScanWidget:
        """The variable-candidate widget (variable-scan ticket 02)."""
        return self._variable_scan

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


class HrDiagramView(_PlaceholderView):
    """H-R diagram view (hr-diagram ticket 03): wraps the
    :class:`~seestar_controller.hr_diagram.view.HrDiagramView` widget — the
    apparent CMD and H-R diagram of the selected crossmatched stack's stars
    (switchable pages, isochrone overlay from the stored isochrone params,
    zoom/pan, error-bar toggle, click a point to highlight the star on the
    stack image and vice versa, export plot PNG + table CSV).

    ``hr_diagram_controller`` is the app's shared
    :class:`~seestar_controller.hr_diagram.HrDiagramController` (following
    the shared viewer controller's project — the currently open project);
    the app window passes it via :func:`build_view`. When omitted, the view
    creates its own, so it also constructs standalone.
    """

    view_id = "hr-diagram"
    title = "H-R diagram"

    def __init__(
        self,
        hr_diagram_controller: HrDiagramController | None = None,
        **kwargs,
    ) -> None:
        # set before super().__init__(): _build_content() runs inside it
        self._hr_diagram_controller = (
            hr_diagram_controller
            if hr_diagram_controller is not None
            else HrDiagramController()
        )
        super().__init__(**kwargs)

    def _build_content(self) -> None:
        self._hr_diagram_widget = HrDiagramViewWidget(self._hr_diagram_controller)
        self._content.append(self._hr_diagram_widget)

    @property
    def hr_diagram_controller(self) -> HrDiagramController:
        """The shared pure-layer H-R diagram controller driving this view."""
        return self._hr_diagram_controller

    @property
    def hr_diagram_widget(self) -> HrDiagramViewWidget:
        """The H-R diagram widget (CMD / H-R pages + stack image)."""
        return self._hr_diagram_widget


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
      :class:`~seestar_controller.observing.queue_editor.QueueEditor`;
    - a **stored-data browser** (offline-analysis ticket 02): one row per
      session of the current project — id, open/closed status, per-kind frame
      counts, and **Stitch mosaic** / **Stack subs** / **Session report**
      buttons — followed by one row per stored frame (kind, id, jd, exposure,
      target), all read from the project store via
      :func:`~seestar_controller.store.list_sessions`
      / :func:`~seestar_controller.store.list_frames`. The browser works in
      offline mode exactly like in connected mode (no live session needed);
      the actions run on the main loop through ``invoke_on_main`` (the
      store connection is main-thread-bound, like every analysis
      controller's store call) and write their products back into the
      project (:func:`~seestar_controller.mosaic.stitch_session` →
      ``mosaics/<session>.fits``,
      :func:`~seestar_controller.stacking.stack_session_subs` → a new
      ``kind='stack'`` frame in ``stacks/<session>/``,
      :func:`~seestar_controller.report.generate_report` →
      ``reports/<session>.html``), reporting the outcome (or the error) in
      the data status label.

    The view re-renders on every controller event (it registers as a
    listener), so changes from anywhere show up.
    """

    view_id = "projects"
    title = "Projects"

    def __init__(
        self,
        project_controller: ProjectController | None = None,
        *,
        invoke_on_main: Callable[[Callable[[], None]], Any] | None = None,
        **kwargs,
    ) -> None:
        # set before super().__init__(): _build_content() runs inside it
        self._project_controller = (
            project_controller
            if project_controller is not None
            else ProjectController(AppState())
        )
        #: marshals the stored-data actions onto the main loop (the app
        #: window binds GLib.idle_add; standalone/tests call directly)
        self._invoke_on_main = invoke_on_main or _direct_invoke
        #: per-row Open buttons, rebuilt with the list
        self._row_buttons: list[Gtk.Button] = []
        #: per-row "current" badges, rebuilt with the list
        self._row_badges: list[Gtk.Label] = []
        #: per-session browser records, rebuilt with the sessions box
        self._session_records: list[dict[str, Any]] = []
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

        # Offline-analysis ticket 02: the stored-data browser of the current
        # project — a section label, one row per session (with the Stitch
        # mosaic / Session report actions) plus one row per stored frame, and
        # a status label for the action outcomes.
        self._data_label = Gtk.Label(
            label="", wrap=True, halign=Gtk.Align.START, xalign=0.0,
            css_classes=["heading"],
        )
        self._content.append(self._data_label)
        self._sessions_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._sessions_box.add_css_class("boxed-list")
        self._content.append(self._sessions_box)
        self._data_status_label = Gtk.Label(
            label="", wrap=True, halign=Gtk.Align.CENTER
        )
        self._content.append(self._data_status_label)

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

    # -- stored-data browser helpers (offline-analysis ticket 02; also used by
    #    the widget tests) -------------------------------------------------------

    @property
    def data_label(self) -> Gtk.Label:
        """The stored-data section label."""
        return self._data_label

    @property
    def sessions_box(self) -> Gtk.ListBox:
        """The sessions/frames browser list."""
        return self._sessions_box

    @property
    def data_status_label(self) -> Gtk.Label:
        """The status label for the Stored-data action buttons (Stitch
        mosaic / Stack subs / Session report)."""
        return self._data_status_label

    def session_count(self) -> int:
        """The number of session rows currently rendered in the browser."""
        return len(self._session_records)

    def session_id(self, index: int) -> str:
        """The session id of the browser row at ``index``."""
        return str(self._session_records[index]["session_id"])

    def session_row_title(self, index: int) -> str:
        """The title label of the session row at ``index``."""
        return self._session_records[index]["title_label"].get_label()

    def session_row_subtitle(self, index: int) -> str:
        """The subtitle (started-at + frame counts) of the session row."""
        return self._session_records[index]["subtitle_label"].get_label()

    def frame_row_count(self, index: int) -> int:
        """The number of frame rows listed under the session at ``index``."""
        return len(self._session_records[index]["frame_labels"])

    def frame_row_label(self, index: int, frame: int) -> str:
        """The label of the ``frame``-th frame row under session ``index``."""
        return self._session_records[index]["frame_labels"][frame]

    def stitch_button(self, index: int) -> Gtk.Button:
        """The Stitch mosaic button of the session at ``index``."""
        return self._session_records[index]["stitch_button"]

    def report_button(self, index: int) -> Gtk.Button:
        """The Session report button of the session at ``index``."""
        return self._session_records[index]["report_button"]

    def click_stitch(self, index: int) -> None:
        """Click the Stitch mosaic button of the session at ``index``."""
        self.stitch_button(index).emit("clicked")

    def click_report(self, index: int) -> None:
        """Click the Session report button of the session at ``index``."""
        self.report_button(index).emit("clicked")

    def stack_subs_button(self, index: int) -> Gtk.Button:
        """The Stack subs button of the session at ``index``."""
        return self._session_records[index]["stack_button"]

    def click_stack_subs(self, index: int) -> None:
        """Click the Stack subs button of the session at ``index``."""
        self.stack_subs_button(index).emit("clicked")

    # -- rendering ------------------------------------------------------------------

    def _on_project_event(self, event: ProjectEvent) -> None:
        """Controller listener: projects and/or current changed -> re-render."""
        self._rebuild()

    def _rebuild(self) -> None:
        """Re-render the banner, root line, project list, and the stored-data
        browser from the controller."""
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
        self._rebuild_data()

    def _rebuild_data(self) -> None:
        """Re-render the stored-data browser (sessions + frames) from the
        current project's store (offline-analysis ticket 02)."""
        while (row := self._sessions_box.get_first_child()) is not None:
            self._sessions_box.remove(row)
        self._session_records = []
        current = self._project_controller.current
        if current is None:
            self._data_label.set_label(
                "Stored data — open a project to browse its sessions & frames"
            )
            return
        self._data_label.set_label(
            f"Stored data — sessions & frames of {current.name} "
            "(read from the project store; works offline)"
        )
        for session in list_sessions(current):
            self._append_session(current, session)

    def _append_session(self, project: Any, session: Any) -> None:
        """Append one session row (id, status, frame counts, Stitch mosaic +
        Session report buttons) plus one row per stored frame to the browser."""
        frames = list_frames(project, session_id=session.id)
        counts = {kind: 0 for kind in KINDS}
        for frame in frames:
            counts[frame.kind] = counts.get(frame.kind, 0) + 1
        status = "open" if session.ended_at is None else "closed"
        title = f"{session.id} — {status}"
        subtitle = (
            f"started {session.started_at} · {counts.get('sub', 0)} subs · "
            f"{counts.get('stack', 0)} stacks · {counts.get('enhanced', 0)} enhanced"
        )
        box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin_top=6,
            margin_bottom=6,
            margin_start=12,
            margin_end=12,
        )
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        title_label = Gtk.Label(label=title, xalign=0.0, valign=Gtk.Align.CENTER)
        subtitle_label = Gtk.Label(
            label=subtitle,
            xalign=0.0,
            valign=Gtk.Align.CENTER,
            css_classes=["dim-label"],
            ellipsize=3,  # Pango.EllipsizeMode.END
        )
        labels.append(title_label)
        labels.append(subtitle_label)
        box.append(labels)
        stitch_button = Gtk.Button(
            label="Stitch mosaic",
            valign=Gtk.Align.CENTER,
            tooltip_text=(
                f"Stitch this session's tile stacks into "
                f"{mosaic_path(project, session.id)} (from the stored frames)"
            ),
        )
        stitch_button.connect(
            "clicked", lambda _btn, sid=session.id: self._on_data_action("stitch", sid)
        )
        stack_button = Gtk.Button(
            label="Stack subs",
            valign=Gtk.Align.CENTER,
            tooltip_text=(
                "Median-combine this session's subs into a new stack frame "
                "(stored under stacks/<session>/, from the stored frames)"
            ),
        )
        stack_button.connect(
            "clicked", lambda _btn, sid=session.id: self._on_data_action("stack_subs", sid)
        )
        report_button = Gtk.Button(
            label="Session report",
            valign=Gtk.Align.CENTER,
            tooltip_text=(
                f"Generate the HTML session report at "
                f"{report_path(project, session.id)} (from the stored data)"
            ),
        )
        report_button.connect(
            "clicked", lambda _btn, sid=session.id: self._on_data_action("report", sid)
        )
        box.append(stitch_button)
        box.append(stack_button)
        box.append(report_button)
        self._sessions_box.append(Gtk.ListBoxRow(child=box, activatable=False))
        frame_labels: list[str] = []
        for frame in frames:
            exposure = f"{frame.exposure:g} s" if frame.exposure is not None else "?"
            target = frame.target or ""
            label = (
                f"    {frame.kind} #{frame.id} · jd {frame.jd:.5f} · "
                f"{exposure} · {target}"
            )
            frame_labels.append(label)
            self._sessions_box.append(
                Gtk.ListBoxRow(
                    child=Gtk.Label(
                        label=label,
                        xalign=0.0,
                        halign=Gtk.Align.START,
                        margin_start=24,
                        css_classes=["dim-label"],
                    ),
                    activatable=False,
                )
            )
        self._session_records.append(
            {
                "session_id": session.id,
                "title_label": title_label,
                "subtitle_label": subtitle_label,
                "stitch_button": stitch_button,
                "stack_button": stack_button,
                "report_button": report_button,
                "frame_labels": frame_labels,
            }
        )

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

    # -- stored-data actions (offline-analysis ticket 02) ---------------------------

    def _set_data_status(self, text: str, *, error: bool) -> None:
        """Set the data status label (errors in red, success in green)."""
        self._data_status_label.set_label(text)
        for css_class in ("error", "success", "dim-label"):
            self._data_status_label.remove_css_class(css_class)
        if error:
            self._data_status_label.add_css_class("error")
        elif text:
            self._data_status_label.add_css_class("success")
        else:
            self._data_status_label.add_css_class("dim-label")

    def _on_data_action(self, kind: str, session_id: str) -> None:
        """Stored-data action button (Stitch mosaic / Stack subs / Session
        report; offline-analysis ticket 02, sub-frames ticket 03): run the
        action on the main loop via ``invoke_on_main`` — the store
        connection is main-thread-bound, exactly like the analysis
        controllers' store calls (the app binds ``GLib.idle_add``) — so the
        action is GLib-main-loop safe and works offline (store only, no link
        layer, no live session). Never raises."""
        self._invoke_on_main(lambda: self._run_data_action(kind, session_id))

    def _run_data_action(self, kind: str, session_id: str) -> None:
        """Run one stored-data action on the current project and report the
        outcome in the data status label; writes the product back into the
        project (``mosaics/<session>.fits`` / a new ``kind='stack'`` frame
        under ``stacks/<session>/`` / ``reports/<session>.html``).
        Never raises — failures are surfaced inline."""
        project = self._project_controller.current
        if project is None:
            self._set_data_status("no project open", error=True)
            return
        try:
            if kind == "stitch":
                path = stitch_session(project, session_id)
                self._set_data_status(
                    f"stitched mosaic of {session_id} → {path}", error=False
                )
            elif kind == "stack_subs":
                result = stack_session_subs(project, session_id)
                self._set_data_status(
                    f"stacked {result.n_subs} subs of {session_id} → "
                    f"stack #{result.frame.id} ({result.frame.path})",
                    error=False,
                )
            else:
                path = generate_report(project, session_id)
                self._set_data_status(
                    f"session report of {session_id} → {path}", error=False
                )
        except Exception as exc:  # noqa: BLE001 - surfaced inline, never raised
            self._set_data_status(
                f"{kind} of {session_id} failed: {exc}", error=True
            )


#: view id -> widget class (one entry per pure-layer view).
VIEW_CLASSES: dict[str, type[_PlaceholderView]] = {
    view.id: cls
    for view, cls in (
        (VIEWS[0], ObserveView),
        (VIEWS[1], EnhanceView),
        (VIEWS[2], PhotometryView),
        (VIEWS[3], LightcurveView),
        (VIEWS[4], HrDiagramView),
        (VIEWS[5], ProjectsView),
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
    hr_diagram_controller: HrDiagramController | None = None,
    mover_controller: MoverController | None = None,
    scan_controller: ScanController | None = None,
    crossmatch_controller: CrossmatchController | None = None,
    solve_controller: SolveController | None = None,
    drift_controller: DriftController | None = None,
    show_toast: Any = None,
    project_controller: ProjectController | None = None,
    project: Any = None,
    app_state: AppState | None = None,
    invoke_on_main: Callable[[Callable[[], None]], Any] | None = None,
) -> _PlaceholderView:
    """Construct the widget for ``view_id``.

    Raises :class:`~seestar_controller.ui.state.UnknownViewError` for an id
    not in the pure-layer view list. ``queue_controller`` /
    ``observe_controller`` / ``project_controller`` / ``project`` /
    ``app_state`` are forwarded to the Observe view (observing tickets 01 +
    03, projects-view ticket 01, visibility-planner ticket 02),
    ``viewer_controller`` /
    ``editor_controller`` to the Enhance view (image-enhance tickets 01 + 03),
    ``crossmatch_controller`` to the Enhance view's cross-match overlay panel
    (gaia-crossmatch ticket 03), ``solve_controller`` / ``show_toast`` to the
    Enhance view's solve panel (astrometry ticket 03 — Solve + Export
    annotated PNG of the stack open in the image viewer), ``drift_controller``
    to the Observe view's drift plot (astrometry ticket 03 — RA/Dec offset
    vs time of the session's solved frames),
    ``picker_controller`` / ``viewer_controller`` / ``suggest_controller``
    / ``run_controller`` to the Photometry view (photometry tickets 01 + 02 +
    04), ``mover_controller`` to the Photometry view's mover overlay/blink
    widget (moving-objects ticket 02), ``scan_controller`` to the Photometry
    view's variable-candidate widget (variable-scan ticket 02),
    ``lightcurve_controller`` /
    ``period_controller`` /
    ``quality_controller`` to the Lightcurve view (period-analysis tickets
    01 + 03 + 04 and frame-quality ticket 02),
    ``hr_diagram_controller`` to the H-R diagram view (hr-diagram ticket
    03), and
    ``project_controller`` to the Projects view (projects-view ticket 01) —
    the app window passes its shared controllers; the other views ignore
    them. ``invoke_on_main`` is forwarded to the Projects view's stored-data
    actions (offline-analysis ticket 02 — the app window binds
    ``GLib.idle_add``, so the Stitch mosaic / Session report actions run on
    the GLib main loop like every other store call).
    """
    view_by_id(view_id)  # validate (raises UnknownViewError)
    if view_id == ObserveView.view_id:
        return ObserveView(
            queue_controller=queue_controller,
            observe_controller=observe_controller,
            drift_controller=drift_controller,
            project_controller=project_controller,
            project=project,
            app_state=app_state,
        )
    if view_id == EnhanceView.view_id:
        return EnhanceView(
            viewer_controller=viewer_controller,
            editor_controller=editor_controller,
            crossmatch_controller=crossmatch_controller,
            solve_controller=solve_controller,
            show_toast=show_toast,
        )
    if view_id == PhotometryView.view_id:
        return PhotometryView(
            picker_controller=picker_controller,
            viewer_controller=viewer_controller,
            suggest_controller=suggest_controller,
            run_controller=run_controller,
            mover_controller=mover_controller,
            scan_controller=scan_controller,
        )
    if view_id == LightcurveView.view_id:
        return LightcurveView(
            lightcurve_controller=lightcurve_controller,
            period_controller=period_controller,
            quality_controller=quality_controller,
        )
    if view_id == HrDiagramView.view_id:
        return HrDiagramView(hr_diagram_controller=hr_diagram_controller)
    if view_id == ProjectsView.view_id:
        return ProjectsView(
            project_controller=project_controller, invoke_on_main=invoke_on_main
        )
    return VIEW_CLASSES[view_id]()
