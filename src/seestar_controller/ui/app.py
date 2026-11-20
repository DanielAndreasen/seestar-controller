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
"""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, Gtk  # noqa: E402

from .state import (  # noqa: E402
    AppState,
    SettingsValidationError,
    load_state,
)
from .views import build_view  # noqa: E402

#: GApplication id.
APP_ID = "io.github.seestar-controller"

#: Window title.
APP_NAME = "Seestar Controller"

#: Sidebar minimum width (pixels).
SIDEBAR_WIDTH = 180


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
    """The main window: header bar + sidebar (ListBox) + view stack.

    The pure-layer :class:`~seestar_controller.ui.state.AppState` is the
    single source of truth: the window renders it (initial view, geometry,
    theme) and writes back to it (view selection, geometry on close).
    """

    def __init__(self, app: Adw.Application, state: AppState) -> None:
        super().__init__(application=app, title=APP_NAME)
        self._state = state

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=APP_NAME))
        toolbar_view.add_top_bar(header)

        # Content: one stack page per view.
        self._stack = Adw.ViewStack()
        for view in state.views:
            self._stack.add_titled(build_view(view.id), view.id, view.title)

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
        self.set_content(toolbar_view)

        # Render the persisted state: geometry, theme, initial view.
        _restore_geometry(self, state.window_geometry())
        apply_dark_theme(state.dark_theme)
        self.select_view(state.current_view_id)

        self.connect("close-request", self._on_close_request)

    @property
    def state(self) -> AppState:
        """The pure-layer app state backing this window."""
        return self._state

    @property
    def view_stack(self) -> Adw.ViewStack:
        """The view stack holding the five view pages."""
        return self._stack

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
        self._state = load_state(settings_path, env=env)

    @property
    def state(self) -> AppState:
        """The pure-layer app state (loaded from the settings file)."""
        return self._state

    def do_startup(self) -> None:
        super().do_startup()
        apply_dark_theme(self._state.dark_theme)

    def do_activate(self) -> None:
        window = SeestarWindow(self, self._state)
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
