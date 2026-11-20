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
:func:`build_view`). The **Projects** view is still a
:class:`_PlaceholderView` (a centered label); later tickets replace the
placeholder content. The
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
"""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib, Gtk  # noqa: E402

from seestar_controller.analysis import (  # noqa: E402
    FOLDED_CSV_FILE,
    AnalysisError,
    ExportError,
    LightcurveController,
    PeriodController,
    export_folded_csv,
    export_lightcurve_csv,
)
from seestar_controller.analysis.lightcurve_view import (  # noqa: E402
    LightcurveView as LightcurveViewWidget,
)
from seestar_controller.analysis.period_view import (  # noqa: E402
    PeriodView as PeriodViewWidget,
)
from seestar_controller.store.csv_export import CSV_FILE as LIGHTCURVE_CSV_FILE  # noqa: E402
from seestar_controller.enhance import EditorController, ViewerController  # noqa: E402
from seestar_controller.enhance.enhance_view import (  # noqa: E402
    EnhanceEditor as EnhanceEditorWidget,
)
from seestar_controller.observing import (  # noqa: E402
    ObserveController,
    PlanQueueController,
)
from seestar_controller.observing.observe_view import (  # noqa: E402
    ObserveView as ObserveViewWidget,
)
from seestar_controller.observing.queue_editor import QueueEditor  # noqa: E402
from seestar_controller.photometry import (  # noqa: E402
    PickController,
    RunController,
    SuggestionController,
)
from seestar_controller.photometry.results_view import (  # noqa: E402
    ResultsPanel as ResultsPanelWidget,
)
from seestar_controller.photometry.star_picker import (  # noqa: E402
    StarPickerView as StarPickerWidget,
)

from .connection import ConnectionController  # noqa: E402
from .settings import SettingsController  # noqa: E402
from .state import VIEWS, AppState, UnknownViewError, view_by_id  # noqa: E402


class _PlaceholderView(Gtk.Box):
    """Base placeholder view: a centered label naming the view.

    Subclasses set :attr:`view_id` / :attr:`title` (mirroring the pure-layer
    :class:`~seestar_controller.ui.state.View`) and may override
    :meth:`_build_content` to append real content instead of the placeholder
    label.
    """

    view_id: str = ""
    title: str = ""

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("orientation", Gtk.Orientation.VERTICAL)
        kwargs.setdefault("spacing", 12)
        kwargs.setdefault("margin_top", 24)
        kwargs.setdefault("margin_bottom", 24)
        kwargs.setdefault("margin_start", 24)
        kwargs.setdefault("margin_end", 24)
        kwargs.setdefault("hexpand", True)
        kwargs.setdefault("vexpand", True)
        super().__init__(**kwargs)
        self._build_content()

    def _build_content(self) -> None:
        label = Gtk.Label(
            label=f"{self.title}\n(placeholder — this view is built by a later ticket)",
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        self.append(label)


class ObserveView(_PlaceholderView):
    """Observe view (observing tickets 01 + 03): wraps the two-pane
    :class:`~seestar_controller.observing.observe_view.ObserveView` widget —
    queue editor + night controls (start/pause/skip/stop/park) on the left,
    live preview + queue progress on the right.

    ``queue_controller`` and ``observe_controller`` are the app's shared
    controllers (the app window passes them via :func:`build_view`); when
    omitted, the view creates its own, so it also constructs standalone.
    ``project`` is the store project the Start button passes to
    ``start_night`` (the Projects view is a later ticket; it can also be set
    later via :meth:`observe_widget.set_project`).
    """

    view_id = "observe"
    title = "Observe"

    def __init__(
        self,
        queue_controller: PlanQueueController | None = None,
        observe_controller: ObserveController | None = None,
        project: Any = None,
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
        # the inner widget handles its own padding
        kwargs.setdefault("margin_top", 0)
        kwargs.setdefault("margin_bottom", 0)
        kwargs.setdefault("margin_start", 0)
        kwargs.setdefault("margin_end", 0)
        kwargs.setdefault("spacing", 0)
        super().__init__(**kwargs)

    def _build_content(self) -> None:
        self._observe_widget = ObserveViewWidget(
            self._observe_controller,
            queue_controller=self._queue_controller,
            project=self._project,
        )
        self.append(self._observe_widget)

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
        # the inner widget handles its own padding
        kwargs.setdefault("margin_top", 0)
        kwargs.setdefault("margin_bottom", 0)
        kwargs.setdefault("margin_start", 0)
        kwargs.setdefault("margin_end", 0)
        kwargs.setdefault("spacing", 0)
        super().__init__(**kwargs)

    def _build_content(self) -> None:
        self._editor = EnhanceEditorWidget(
            self._viewer_controller, self._editor_controller
        )
        self.append(self._editor)

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
        # the inner widget handles its own padding
        kwargs.setdefault("margin_top", 0)
        kwargs.setdefault("margin_bottom", 0)
        kwargs.setdefault("margin_start", 0)
        kwargs.setdefault("margin_end", 0)
        kwargs.setdefault("spacing", 0)
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
        self.append(paned)

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
    """Lightcurve view (period-analysis tickets 01 + 03 + 04): wraps the
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
    ``invoke_on_main``) plus a status label for export results.

    ``lightcurve_controller`` is the app's shared
    :class:`~seestar_controller.analysis.lightcurve.LightcurveController`
    (following the shared viewer controller's project — the currently open
    project) and ``period_controller`` the app's shared
    :class:`~seestar_controller.analysis.fold.PeriodController` bound to that
    same lightcurve controller (so both plots follow the open project); the
    app window passes both via :func:`build_view`. When omitted, the view
    creates its own, so it also constructs standalone.
    """

    view_id = "lightcurve"
    title = "Lightcurve"

    def __init__(
        self,
        lightcurve_controller: LightcurveController | None = None,
        period_controller: PeriodController | None = None,
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
        # the inner widgets handle their own padding
        kwargs.setdefault("margin_top", 0)
        kwargs.setdefault("margin_bottom", 0)
        kwargs.setdefault("margin_start", 0)
        kwargs.setdefault("margin_end", 0)
        kwargs.setdefault("spacing", 0)
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
        self.append(toolbar)
        self._lightcurve_widget = LightcurveViewWidget(self._lightcurve_controller)
        self.append(self._lightcurve_widget)
        self._period_widget = PeriodViewWidget(self._period_controller)
        self.append(self._period_widget)

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
    def export_csv_button(self) -> Gtk.Button:
        """The Export CSV button (period-analysis ticket 04)."""
        return self._export_csv_button

    @property
    def export_folded_csv_button(self) -> Gtk.Button:
        """The Export folded CSV button (period-analysis ticket 04)."""
        return self._export_folded_csv_button


class ProjectsView(_PlaceholderView):
    """Projects view placeholder (list/create/open come later)."""

    view_id = "projects"
    title = "Projects"


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
    project: Any = None,
) -> _PlaceholderView:
    """Construct the widget for ``view_id``.

    Raises :class:`~seestar_controller.ui.state.UnknownViewError` for an id
    not in the pure-layer view list. ``queue_controller`` /
    ``observe_controller`` / ``project`` are forwarded to the Observe view
    (observing tickets 01 + 03), ``viewer_controller`` /
    ``editor_controller`` to the Enhance view (image-enhance tickets 01 + 03),
    ``picker_controller`` / ``viewer_controller`` / ``suggest_controller``
    / ``run_controller`` to the Photometry view (photometry tickets 01 + 02 +
    04), and ``lightcurve_controller`` / ``period_controller`` to the
    Lightcurve view (period-analysis tickets 01 + 03 + 04) — the app window
    passes its shared controllers; the other views ignore them.
    """
    view_by_id(view_id)  # validate (raises UnknownViewError)
    if view_id == ObserveView.view_id:
        return ObserveView(
            queue_controller=queue_controller,
            observe_controller=observe_controller,
            project=project,
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
        )
    return VIEW_CLASSES[view_id]()
