"""The catalog-assist suggestions panel — the WIDGET layer of photometry
ticket 02 (:mod:`seestar_controller.photometry.suggestions_panel`).

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by the pure layer
(:mod:`seestar_controller.photometry.suggest` / :mod:`.picking`) or eagerly by
the package ``__init__`` — the pure layer stays gi-free and headless-testable
(see docs/wiki/assumptions.md: the sandbox has no GTK). It is re-exported as
``photometry.SuggestionsPanel`` lazily (PEP 562 ``__getattr__``).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

:class:`SuggestionsPanel` is a thin observer over the pure
:class:`~seestar_controller.photometry.suggest.SuggestionController` (the app
owns one shared instance, bound to the shared
:class:`~seestar_controller.photometry.picking.PickController` and configured
with the comp-search radius from the settings; the window passes it down via
:func:`seestar_controller.ui.views.build_view`):

- a **Search** button runs the catalog cone search — the pure
  :meth:`~seestar_controller.photometry.suggest.SuggestionController.refresh`
  BLOCKS on the catalog lookup (network), so the panel runs it on a daemon
  worker thread and marshals the listener notifications back to the GLib main
  loop with :func:`GLib.idle_add`; the panel also registers its worker-thread
  :meth:`search` as the controller's ``search_runner``, so the AUTOMATIC
  searches (target pick changed, radius changed) go through the worker thread
  too — never on the main loop;
- a **sort dropdown** next to Search (issue 07): nearest-first (the default,
  the historical order), brightest-first, or closest-in-magnitude-to-the
  target pick — it sets the controller's
  :attr:`~seestar_controller.photometry.suggest.SuggestionController.sort_mode`,
  which re-sorts the current suggestions client-side (no new catalog lookup);
- one :class:`Gtk.ListBox` row per suggestion (name, band/mag, distance from
  the target) with an **Add** button — clicking it calls
  :meth:`~seestar_controller.photometry.suggest.SuggestionController.add_suggestion`,
  which converts the star's sky position to a pixel via the current frame's
  WCS and adds it as a comp pick (the star picker re-renders through the
  shared pick controller). Issue 08: a suggestion whose sky position falls
  OUTSIDE the open frame's footprint (``in_frame=False``) is rendered greyed
  with its Add button disabled — never offered as an addable comp;
- a status label: the clean catalog error (e.g. offline with no cache) in the
  ``error`` style, or the suggestion count (plus how many fall outside the
  frame's FOV) / "pick a target first" hint; the radius label also shows the
  WCS-derived FOV radius of the open frame when solved.

The panel re-renders on every frozen
:class:`~seestar_controller.photometry.suggest.SuggestionSnapshot`.
"""

from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib, Gtk

from .suggest import (
    SORT_MODES,
    CompSuggestion,
    SuggestionController,
    SuggestionError,
    SuggestionSnapshot,
)

#: The sort dropdown's labels, in :data:`~.suggest.SORT_MODES` order
#: ("distance", "magnitude", "closest_mag" — issue 07).
SORT_LABELS = ("Nearest first", "Brightest first", "Closest mag to target")


class SuggestionsPanel(Gtk.Box):
    """The catalog-assist panel: Search button + sort dropdown (issue 07) +
    suggestions list (one Add button per row) + status label, observing a
    :class:`~seestar_controller.photometry.suggest.SuggestionController`.

    Constructed with the shared suggestion controller (the app passes it; a
    fresh standalone one is created when omitted, so the widget also works
    alone).
    """

    def __init__(
        self,
        suggest_controller: SuggestionController | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("orientation", Gtk.Orientation.VERTICAL)
        kwargs.setdefault("spacing", 6)
        super().__init__(**kwargs)
        if suggest_controller is None:
            suggest_controller = SuggestionController()
        self._controller = suggest_controller
        self._main_thread_id = threading.get_ident()
        self._search_thread: threading.Thread | None = None
        # This panel drives the automatic searches (target pick changed, radius
        # changed): they run on the worker thread below, never on the GLib
        # main loop (the pure refresh() blocks on the catalog lookup).
        suggest_controller.search_runner = self.search

        heading = Gtk.Label(label="Catalog comps", xalign=0.0, css_classes=["heading"])
        self.append(heading)

        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._search_button = Gtk.Button(
            label="Search",
            tooltip_text=(
                "List the standard stars near the target pick from the catalog "
                "(within the configured comp-search radius)"
            ),
        )
        self._search_button.connect("clicked", self._on_search_clicked)
        search_row.append(self._search_button)
        # issue 07: sort control next to Search — nearest-first (default),
        # brightest-first, or closest in magnitude to the target pick
        self._sort_dropdown = Gtk.DropDown(
            model=Gtk.StringList.new(list(SORT_LABELS)),
            selected=SORT_MODES.index(suggest_controller.sort_mode),
        )
        self._sort_dropdown.set_tooltip_text(
            "Sort the catalog comp suggestions (re-sorts the current list, no new search)"
        )
        self._sort_dropdown.connect("notify::selected", self._on_sort_selected)
        search_row.append(self._sort_dropdown)
        self._radius_label = Gtk.Label(
            label=self._radius_text(),
            xalign=0.0,
            hexpand=True,
            css_classes=["dim-label", "caption"],
        )
        search_row.append(self._radius_label)
        self.append(search_row)

        self._list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        scrolled = Gtk.ScrolledWindow(
            child=self._list_box,
            min_content_width=260,
            min_content_height=60,
            max_content_height=180,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        self.append(scrolled)

        self._status_label = Gtk.Label(label="", wrap=True, halign=Gtk.Align.CENTER)
        self.append(self._status_label)

        self._controller.add_listener(self._on_snapshot)
        self._render(self._controller.snapshot)

    # -- public helpers (also used by the widget tests) ------------------------------

    @property
    def controller(self) -> SuggestionController:
        """The pure-layer suggestion controller backing this panel."""
        return self._controller

    @property
    def suggest_controller(self) -> SuggestionController:
        """Alias of :attr:`controller`."""
        return self._controller

    @property
    def list_box(self) -> Gtk.ListBox:
        """The suggestions list."""
        return self._list_box

    @property
    def search_button(self) -> Gtk.Button:
        """The Search button (starts the catalog lookup on a worker thread)."""
        return self._search_button

    @property
    def sort_dropdown(self) -> Gtk.DropDown:
        """The sort dropdown next to Search (issue 07): its selection maps to
        :data:`~.suggest.SORT_MODES` via :data:`SORT_LABELS`."""
        return self._sort_dropdown

    @property
    def status_label(self) -> Gtk.Label:
        """The status label (suggestion count / clean catalog error)."""
        return self._status_label

    @property
    def radius_label(self) -> Gtk.Label:
        """The label showing the configured comp-search radius."""
        return self._radius_label

    def suggestion_rows(self) -> list[Gtk.ListBoxRow]:
        """The current suggestion rows (each carries ``suggestion_index`` and
        an Add button in its ``add_button`` attribute)."""
        rows: list[Gtk.ListBoxRow] = []
        index = 0
        while (row := self._list_box.get_row_at_index(index)) is not None:
            rows.append(row)
            index += 1
        return rows

    def run_search(self) -> SuggestionSnapshot:
        """Run the catalog search SYNCHRONOUSLY (blocks on the lookup) and
        return the new snapshot — this is what the worker thread runs; tests
        may call it directly."""
        return self._controller.refresh()

    def search(self) -> threading.Thread:
        """Start the catalog search on a daemon worker thread (the pure
        ``refresh()`` blocks on the network); returns the thread, so callers
        (tests) can ``join()`` it. Re-entrant: a search already running is not
        restarted."""
        if self._search_thread is not None and self._search_thread.is_alive():
            return self._search_thread
        thread = threading.Thread(target=self.run_search, name="photometry-comp-search", daemon=True)
        self._search_thread = thread
        thread.start()
        return thread

    # -- internals ---------------------------------------------------------------------

    def _radius_text(self, snapshot: SuggestionSnapshot | None = None) -> str:
        """The radius label text: the configured comp-search radius, plus the
        open frame's WCS-derived FOV radius when solved (issue 08)."""
        text = f"radius {self._controller.radius_deg:g}°"
        fov = (
            snapshot.frame_fov_deg
            if snapshot is not None
            else self._controller.frame_fov_radius_deg
        )
        if fov is not None:
            text += f" · frame FOV {fov:.3g}°"
        return text

    def _on_search_clicked(self, *_args) -> None:
        self._set_status("searching the catalog…", error=False)
        self.search()

    def _on_sort_selected(self, dropdown: Gtk.DropDown, *_args) -> None:
        """Sort dropdown handler (issue 07): map the selected label to a
        :data:`~.suggest.SORT_MODES` mode and set it on the controller — the
        setter re-sorts the current suggestions client-side and notifies, so
        the rows re-render in the new order (no new catalog lookup)."""
        index = int(dropdown.get_selected())
        if not 0 <= index < len(SORT_MODES):
            return
        mode = SORT_MODES[index]
        if mode != self._controller.sort_mode:
            self._controller.sort_mode = mode

    def _on_add_clicked(self, button: Gtk.Button, *_args) -> None:
        index = getattr(button, "suggestion_index", None)
        if index is None:
            return
        try:
            spec = self._controller.add_suggestion(index)
        except SuggestionError as exc:
            self._set_status(str(exc), error=True)
            return
        self._set_status(f"added comp {spec.describe()}", error=False)

    def _on_snapshot(self, snapshot: SuggestionSnapshot) -> None:
        """Suggestion-controller listener: render on the GLib main loop (the
        search runs on a worker thread, so marshal with ``GLib.idle_add``;
        on the main thread render directly)."""
        if threading.get_ident() == self._main_thread_id:
            self._render(snapshot)
        else:
            GLib.idle_add(self._render, snapshot)

    def _render(self, snapshot: SuggestionSnapshot) -> bool:
        """Render a frozen snapshot: rebuild the suggestion rows and the
        status label. Returns ``GLib.SOURCE_REMOVE`` (one-shot idle callback)."""
        self._radius_label.set_label(self._radius_text(snapshot))
        # keep the sort dropdown in sync with the controller's sort mode
        # (e.g. when it was changed elsewhere); setting the same value is a
        # no-op for the notify::selected handler
        if snapshot.sort_mode in SORT_MODES:
            selected = SORT_MODES.index(snapshot.sort_mode)
            if int(self._sort_dropdown.get_selected()) != selected:
                self._sort_dropdown.set_selected(selected)
        while (row := self._list_box.get_row_at_index(0)) is not None:
            self._list_box.remove(row)
        for index, suggestion in enumerate(snapshot.suggestions):
            self._list_box.append(self._make_row(index, suggestion))
        if snapshot.error is not None:
            self._set_status(snapshot.error, error=True)
        elif snapshot.target is None:
            self._set_status("pick a target first — then search for comps", error=False)
        else:
            n = len(snapshot.suggestions)
            text = f"{n} standard(s) within {snapshot.radius_deg:g}° of the target"
            if snapshot.out_of_frame_count:
                text += f" ({snapshot.out_of_frame_count} outside the frame)"
            self._set_status(text, error=False)
        return GLib.SOURCE_REMOVE

    def _make_row(self, index: int, suggestion: CompSuggestion) -> Gtk.ListBoxRow:
        """One suggestion row: the description label + an Add button. Issue
        08: an out-of-frame suggestion (``in_frame=False``) is greyed and its
        Add button disabled — it is listed for context but never addable."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        box.set_margin_start(8)
        box.set_margin_end(8)
        label = Gtk.Label(label=suggestion.describe(), xalign=0.0, hexpand=True, wrap=True)
        if not suggestion.in_frame:
            label.add_css_class("dim-label")
        box.append(label)
        add_button = Gtk.Button(
            label="Add",
            tooltip_text="Add this standard star as a comparison-star pick",
        )
        add_button.set_valign(Gtk.Align.CENTER)
        if not suggestion.in_frame:
            # issue 08: outside the open frame's FOV — not offered as an
            # addable comp (the pure layer refuses it too)
            add_button.set_sensitive(False)
            add_button.set_tooltip_text(
                "Outside the open frame's footprint (FOV) — not addable as a comp"
            )
        add_button.suggestion_index = index  # type: ignore[attr-defined]
        add_button.connect("clicked", self._on_add_clicked)
        box.append(add_button)
        row = Gtk.ListBoxRow(child=box)
        row.suggestion_index = index  # type: ignore[attr-defined]
        row.add_button = add_button  # type: ignore[attr-defined]
        row.in_frame = suggestion.in_frame  # type: ignore[attr-defined]
        return row

    def _set_status(self, text: str, *, error: bool) -> None:
        self._status_label.set_label(text)
        for css_class in ("error", "dim-label"):
            self._status_label.remove_css_class(css_class)
        if error:
            self._status_label.add_css_class("error")
        else:
            self._status_label.add_css_class("dim-label")
