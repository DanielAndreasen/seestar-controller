"""The cross-match overlay view widget — the WIDGET layer of gaia-crossmatch
issue 03 (:mod:`seestar_controller.gaia.overlay_view`).

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by the pure layer
(:mod:`seestar_controller.gaia.overlay`) or eagerly by the package
``__init__`` — the pure layer stays gi-free and headless-testable (see
docs/wiki/assumptions.md: the sandbox has no GTK). It is re-exported as
``gaia.CrossmatchOverlayView`` lazily (PEP 562 ``__getattr__``).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

:class:`CrossmatchOverlayView` is a thin observer over the pure
:class:`~seestar_controller.gaia.overlay.CrossmatchController` (the app owns
one shared instance, following the shared viewer controller; the window
passes it down via :func:`seestar_controller.ui.views.build_view` to the
Enhance view, which embeds this widget below the image editor — the
cross-match panel of the image viewer):

- a **header row** — heading, the open stack's label, a **match-radius**
  :class:`Gtk.SpinButton` (arcsec) and a **G-limit** :class:`Gtk.SpinButton`;
  changing either re-runs the cross-match on a worker thread (the Gaia query
  is cache-first, so a radius-only change does no HTTP at all), plus a
  **Cross-match** button running the full pipeline;
- the **annotated stack** (:class:`Gtk.Picture`, PIL -> PNG bytes ->
  :class:`Gdk.Texture`, the same pattern as the Enhance view's preview):
  the stack tone-mapped with every matched detection ringed and labelled
  with its Gaia G magnitude and every unmatched detection flagged with a
  distinct X marker;
- a **status label** (``"4/5 matched · r=1.5″ · G<20"``, or a clean error
  message).

The view re-renders on every frozen
:class:`~seestar_controller.gaia.overlay.CrossmatchSnapshot` (listener
notifications marshalled onto the GLib main loop when they come from a
worker thread).
"""

from __future__ import annotations

import io
import threading
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gdk, GLib, Gtk
from PIL import Image

from .overlay import (
    MAX_G_LIMIT,
    MAX_MATCH_RADIUS_ARCSEC,
    MIN_G_LIMIT,
    MIN_MATCH_RADIUS_ARCSEC,
    CrossmatchController,
    CrossmatchSnapshot,
)


def texture_from_pil(image: Image.Image) -> Gdk.Texture:
    """Build a :class:`Gdk.Texture` from a PIL image (PNG bytes ->
    ``Gdk.Texture.new_from_bytes`` — the same pattern as the Enhance view's
    preview)."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Gdk.Texture.new_from_bytes(GLib.Bytes(buffer.getvalue()))


class CrossmatchOverlayView(Gtk.Box):
    """The cross-match overlay panel of the image viewer: stack label +
    radius/G-limit controls + Cross-match button on top, the annotated stack
    below, and a status label — observing a
    :class:`~seestar_controller.gaia.overlay.CrossmatchController`."""

    def __init__(
        self,
        crossmatch_controller: CrossmatchController | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("orientation", Gtk.Orientation.VERTICAL)
        kwargs.setdefault("spacing", 6)
        super().__init__(**kwargs)
        if crossmatch_controller is None:
            crossmatch_controller = CrossmatchController()
        self._controller = crossmatch_controller
        self._main_thread_id = threading.get_ident()
        self._busy = False
        self._syncing = False

        # header row: heading + stack label + radius + G limit + Cross-match
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.append(Gtk.Label(label="Cross-match", xalign=0.0, css_classes=["heading"]))
        self._stack_label = Gtk.Label(
            label="no stack open",
            xalign=0.0,
            hexpand=True,
            css_classes=["dim-label"],
            tooltip_text="The stack open in the image viewer (the cross-match target)",
        )
        header.append(self._stack_label)
        header.append(Gtk.Label(label="r (\u2033)", tooltip_text="Match radius in arcseconds"))
        self._radius_spin = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(
                value=self._controller.radius_arcsec,
                lower=MIN_MATCH_RADIUS_ARCSEC,
                upper=MAX_MATCH_RADIUS_ARCSEC,
                step_increment=0.1,
                page_increment=0.5,
            ),
            digits=1,
            tooltip_text="Match radius in arcseconds — changing it re-runs the "
            "cross-match (the Gaia query is cache-first)",
        )
        self._radius_spin.connect("value-changed", self._on_radius_changed)
        header.append(self._radius_spin)
        header.append(Gtk.Label(label="G <", tooltip_text="Gaia G magnitude limit"))
        self._g_limit_spin = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(
                value=self._controller.g_limit,
                lower=MIN_G_LIMIT,
                upper=MAX_G_LIMIT,
                step_increment=0.5,
                page_increment=1.0,
            ),
            digits=1,
            tooltip_text="Gaia G magnitude limit — changing it re-queries Gaia DR3 "
            "(served from the disk cache when possible) and re-matches",
        )
        self._g_limit_spin.connect("value-changed", self._on_g_limit_changed)
        header.append(self._g_limit_spin)
        self._run_button = Gtk.Button(
            label="Cross-match",
            tooltip_text="Detect the sources on the open stack, query Gaia DR3 "
            "around the field (cache-first), cross-match, and save the table",
        )
        self._run_button.connect("clicked", self._on_run_clicked)
        header.append(self._run_button)
        self.append(header)

        # the annotated stack (or a placeholder)
        self._stack = Gtk.Stack()
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)
        self._picture = Gtk.Picture()
        self._picture.set_can_shrink(True)
        self._picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._stack.add_titled(self._picture, "image", "Overlay")
        self._placeholder_label = Gtk.Label(
            label="No stack open — open a stack in the image viewer",
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            wrap=True,
            css_classes=["dim-label"],
        )
        self._stack.add_titled(self._placeholder_label, "empty", "Empty")
        self._stack.set_visible_child_name("empty")
        self.append(self._stack)

        # status label (counts / clean error)
        self._status_label = Gtk.Label(label="", wrap=True, halign=Gtk.Align.CENTER)
        self.append(self._status_label)

        self.connect("destroy", self._on_destroy)
        self._controller.add_listener(self._on_snapshot)
        self._render(self._controller.snapshot)

    # -- public helpers (also used by the widget tests) ------------------------------

    @property
    def controller(self) -> CrossmatchController:
        """The pure-layer cross-match controller backing this view."""
        return self._controller

    @property
    def crossmatch_controller(self) -> CrossmatchController:
        """Alias of :attr:`controller`."""
        return self._controller

    @property
    def stack_label(self) -> Gtk.Label:
        """The open-stack label."""
        return self._stack_label

    @property
    def radius_spin(self) -> Gtk.SpinButton:
        """The match-radius spin button (arcsec)."""
        return self._radius_spin

    @property
    def g_limit_spin(self) -> Gtk.SpinButton:
        """The G-limit spin button."""
        return self._g_limit_spin

    @property
    def run_button(self) -> Gtk.Button:
        """The Cross-match button."""
        return self._run_button

    @property
    def picture(self) -> Gtk.Picture:
        """The annotated-stack picture."""
        return self._picture

    @property
    def status_label(self) -> Gtk.Label:
        """The status label (counts / clean error)."""
        return self._status_label

    # -- handlers -------------------------------------------------------------------------

    def _on_run_clicked(self, *_args: Any) -> None:
        self._run_async(lambda: self._controller.run_crossmatch())

    def _on_radius_changed(self, spin: Gtk.SpinButton) -> None:
        if self._syncing:
            return
        self._run_async(lambda: self._controller.set_radius_arcsec(spin.get_value()))

    def _on_g_limit_changed(self, spin: Gtk.SpinButton) -> None:
        if self._syncing:
            return
        self._run_async(lambda: self._controller.set_g_limit(spin.get_value()))

    def _on_destroy(self, *_args: Any) -> None:
        self._controller.remove_listener(self._on_snapshot)

    def _run_async(self, fn: Any) -> None:
        """Run ``fn`` on a daemon worker thread; the busy state is cleared on
        the main loop when it finishes."""
        if self._busy:
            return
        self._busy = True
        self._run_button.set_sensitive(False)

        def work() -> None:
            try:
                fn()
            finally:
                GLib.idle_add(self._on_work_done)

        threading.Thread(target=work, daemon=True).start()

    def _on_work_done(self) -> bool:
        self._busy = False
        self._run_button.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    # -- controller listener ---------------------------------------------------------------

    def _on_snapshot(self, snapshot: CrossmatchSnapshot) -> None:
        """Controller listener: render on the GLib main loop (notifications
        may come from a worker thread, so marshal with ``GLib.idle_add``; on
        the main thread render directly)."""
        if threading.get_ident() == self._main_thread_id:
            self._render(snapshot)
        else:
            GLib.idle_add(self._render, snapshot)

    def _render(self, snapshot: CrossmatchSnapshot) -> bool:
        """Render a frozen snapshot: the controls (without re-triggering
        them), the stack label, the annotated stack, and the status.
        Returns ``GLib.SOURCE_REMOVE`` (one-shot idle callback)."""
        self._syncing = True
        try:
            if abs(self._radius_spin.get_value() - snapshot.radius_arcsec) > 1e-9:
                self._radius_spin.set_value(snapshot.radius_arcsec)
            if abs(self._g_limit_spin.get_value() - snapshot.g_limit) > 1e-9:
                self._g_limit_spin.set_value(snapshot.g_limit)
        finally:
            self._syncing = False
        self._stack_label.set_label(snapshot.stack_label or "no stack open")
        if snapshot.image is not None:
            self._picture.set_paintable(texture_from_pil(snapshot.image))
            self._stack.set_visible_child_name("image")
        else:
            self._stack.set_visible_child_name("empty")
        if snapshot.error is not None:
            self._status_label.set_label(snapshot.error)
            self._status_label.add_css_class("error")
        else:
            self._status_label.remove_css_class("error")
            self._status_label.set_label(snapshot.status or "")
        return GLib.SOURCE_REMOVE
