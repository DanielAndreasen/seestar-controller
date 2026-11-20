"""View widgets for the ui shell — the WIDGET layer.

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by :mod:`seestar_controller.ui.state`
or eagerly by the package ``__init__`` — the pure layer stays gi-free and
headless-testable (see docs/wiki/assumptions.md: the sandbox has no GTK).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

Most views are still :class:`_PlaceholderView`\\ s (a centered label); later
tickets (image-enhance, photometry, period-analysis) replace the placeholder
content. The **Observe** view (observing ticket 01) already carries the real
content: a :class:`~seestar_controller.observing.queue_editor.QueueEditor`
driven by the app's shared
:class:`~seestar_controller.observing.queue_controller.PlanQueueController`
(passed in by the app window via :func:`build_view`; a fresh controller is
created when omitted, so the view also constructs standalone).
:data:`VIEW_CLASSES` maps the pure-layer view ids
(:mod:`seestar_controller.ui.state`) to the widget classes, and
:func:`build_view` is the factory the app window uses.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk  # noqa: E402

from seestar_controller.observing import PlanQueueController  # noqa: E402
from seestar_controller.observing.queue_editor import QueueEditor  # noqa: E402

from .state import VIEWS, UnknownViewError, view_by_id  # noqa: E402


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
    """Observe view: the plan/queue editor (observing ticket 01) plus a
    preview placeholder (live preview + queue progress come in later
    observing tickets).

    ``queue_controller`` is the app's shared
    :class:`~seestar_controller.observing.queue_controller.PlanQueueController`
    (the app window passes it); when omitted, the view creates its own.
    """

    view_id = "observe"
    title = "Observe"

    def __init__(
        self, queue_controller: PlanQueueController | None = None, **kwargs
    ) -> None:
        # set before super().__init__(): _build_content() runs inside it
        self._queue_controller = (
            queue_controller if queue_controller is not None else PlanQueueController()
        )
        super().__init__(**kwargs)

    def _build_content(self) -> None:
        self._queue_editor = QueueEditor(self._queue_controller)
        self.append(self._queue_editor)
        preview = Gtk.Label(
            label="Live preview + queue progress come in later observing tickets",
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            vexpand=True,
            css_classes=["dim-label"],
        )
        self.append(preview)

    @property
    def queue_controller(self) -> PlanQueueController:
        """The shared plan-queue controller backing this view."""
        return self._queue_controller

    @property
    def queue_editor(self) -> QueueEditor:
        """The queue editor widget."""
        return self._queue_editor


class EnhanceView(_PlaceholderView):
    """Enhance view placeholder (image + sliders come later)."""

    view_id = "enhance"
    title = "Enhance"


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
    view_id: str, *, queue_controller: PlanQueueController | None = None
) -> _PlaceholderView:
    """Construct the widget for ``view_id``.

    Raises :class:`~seestar_controller.ui.state.UnknownViewError` for an id
    not in the pure-layer view list. ``queue_controller`` is forwarded to the
    Observe view (observing ticket 01) — the app window passes its shared
    plan-queue controller; the other views ignore it.
    """
    view_by_id(view_id)  # validate (raises UnknownViewError)
    if view_id == ObserveView.view_id:
        return ObserveView(queue_controller=queue_controller)
    return VIEW_CLASSES[view_id]()
