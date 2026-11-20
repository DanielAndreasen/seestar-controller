"""Placeholder view widgets for the ui shell — the WIDGET layer.

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by :mod:`seestar_controller.ui.state`
or eagerly by the package ``__init__`` — the pure layer stays gi-free and
headless-testable (see docs/wiki/assumptions.md: the sandbox has no GTK).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

Each view is a :class:`Gtk.Box` with a placeholder label for now; later
tickets (observing, image-enhance, photometry, period-analysis) replace the
placeholder content. :data:`VIEW_CLASSES` maps the pure-layer view ids
(:mod:`seestar_controller.ui.state`) to the widget classes, and
:func:`build_view` is the factory the app window uses.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk  # noqa: E402

from .state import VIEWS, UnknownViewError, view_by_id  # noqa: E402


class _PlaceholderView(Gtk.Box):
    """Base placeholder view: a centered label naming the view.

    Subclasses set :attr:`view_id` / :attr:`title` (mirroring the pure-layer
    :class:`~seestar_controller.ui.state.View`).
    """

    view_id: str = ""
    title: str = ""

    def __init__(self) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=24,
            margin_bottom=24,
            margin_start=24,
            margin_end=24,
            hexpand=True,
            vexpand=True,
        )
        label = Gtk.Label(
            label=f"{self.title}\n(placeholder — this view is built by a later ticket)",
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        self.append(label)


class ObserveView(_PlaceholderView):
    """Observe view placeholder (plan/queue + preview come later)."""

    view_id = "observe"
    title = "Observe"


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


def build_view(view_id: str) -> _PlaceholderView:
    """Construct the placeholder widget for ``view_id``.

    Raises :class:`~seestar_controller.ui.state.UnknownViewError` for an id
    not in the pure-layer view list.
    """
    view_by_id(view_id)  # validate (raises UnknownViewError)
    return VIEW_CLASSES[view_id]()
