"""View widgets for the ui shell — the WIDGET layer.

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by :mod:`seestar_controller.ui.state`
or eagerly by the package ``__init__`` — the pure layer stays gi-free and
headless-testable (see docs/wiki/assumptions.md: the sandbox has no GTK).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

Most views are still :class:`_PlaceholderView`\\ s (a centered label); later
tickets (photometry, period-analysis) replace the placeholder content. The
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
shows a "no project" placeholder — the Projects view is a later ticket).
:data:`VIEW_CLASSES` maps the pure-layer view ids
(:mod:`seestar_controller.ui.state`) to the widget classes, and
:func:`build_view` is the factory the app window uses.
"""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk  # noqa: E402

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
    """Photometry view placeholder (star picking + run come later)."""

    view_id = "photometry"
    title = "Photometry"


class LightcurveView(_PlaceholderView):
    """Lightcurve view placeholder (plots come later)."""

    view_id = "lightcurve"
    title = "Lightcurve"


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
    project: Any = None,
) -> _PlaceholderView:
    """Construct the widget for ``view_id``.

    Raises :class:`~seestar_controller.ui.state.UnknownViewError` for an id
    not in the pure-layer view list. ``queue_controller`` /
    ``observe_controller`` / ``project`` are forwarded to the Observe view
    (observing tickets 01 + 03) and ``viewer_controller`` /
    ``editor_controller`` to the Enhance view (image-enhance tickets 01 + 03)
    — the app window passes its shared controllers; the other views ignore
    them.
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
    return VIEW_CLASSES[view_id]()
