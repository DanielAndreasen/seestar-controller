"""The night-timeline view widget — the WIDGET layer of visibility-planner
ticket 02 (:mod:`seestar_controller.visibility.timeline_view`).

This module imports ``gi`` at module top (it is a widget module, the only
place allowed to): it is never imported by the pure layer
(:mod:`seestar_controller.visibility.planner` / :mod:`.timeline` /
:mod:`.render`) or eagerly by the package ``__init__`` — the pure layer stays
gi-free and headless-testable (see docs/wiki/assumptions.md). It is re-exported
as ``visibility.VisibilityView`` lazily (PEP 562 ``__getattr__``).

On the desktop the system packages are required (PyGObject is NOT a pip
dependency — it needs the system GTK headers to build)::

    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

:class:`VisibilityView` is a thin observer over the pure
:mod:`seestar_controller.visibility.timeline` / :mod:`.render` layers and the
observing plan-queue controller (duck-typed — the app passes its shared
:class:`~seestar_controller.observing.queue_controller.PlanQueueController`;
the Observe page wires it in):

- a **header row** — heading, the night date, a **Refresh** button and an
  **Export PNG** button (:class:`Gtk.FileDialog` save dialog; the programmatic
  :meth:`VisibilityView.export_png` drives the same write without the dialog);
- the **timeline plot** on a :class:`Gtk.DrawingArea`: the pure
  :func:`~seestar_controller.visibility.render.render_timeline_png` renders the
  :class:`~seestar_controller.visibility.timeline.TimelineModel` with
  matplotlib's **Agg** backend to PNG bytes, the PNG goes through **PIL** into a
  :class:`Gdk.Texture` (``Gdk.Texture.new_from_bytes`` — the same PNG -> PIL ->
  texture path as the image-enhance viewer), and the draw func paints it scaled
  to the widget's allocation. The plot shows one altitude curve per queued
  target, the astronomical-twilight shading (dusk/dawn annotated), the horizon,
  and the moon track with its illuminated fraction;
- the **suggested order** list (one row per queued entry, in the suggested
  order, from :func:`~seestar_controller.visibility.timeline.suggested_labels`)
  with an **Apply suggested order** button: it writes the reordered entries
  back to the observing PlanQueue through the queue controller's public API
  (:func:`~seestar_controller.visibility.timeline.apply_order` — a sequence of
  ``move(index, to_index)`` calls, each notifying the queue's listeners, so the
  Observe view's queue editor re-renders too);
- an **empty-state placeholder** (:class:`Gtk.Stack`) when the queue is empty
  or the site is not configured (settings), with the reason in the status line.

The view re-renders on every queue-changed event (it registers as a queue
listener; events from a worker thread are marshalled onto the GLib main loop)
and on Refresh.
"""

from __future__ import annotations

import datetime
import io
import threading
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import cairo
import numpy as np
from astropy import units as u
from gi.repository import Adw, Gdk, GLib, Gtk
from PIL import Image

from seestar_controller.visibility.render import (
    DEFAULT_HEIGHT_PX,
    DEFAULT_WIDTH_PX,
    render_timeline_png,
)
from seestar_controller.visibility.site import Site, VisibilityError
from seestar_controller.visibility.timeline import (
    SettingsSource,
    TimelineModel,
    apply_order,
    build_timeline,
    format_local,
    resolve_settings,
    suggested_labels,
)


def texture_from_pil(image: Image.Image) -> Gdk.Texture:
    """Build a :class:`Gdk.Texture` from a PIL image (PNG bytes ->
    ``Gdk.Texture.new_from_bytes`` — the same pattern as the Enhance viewer and
    the Observe view's preview)."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Gdk.Texture.new_from_bytes(GLib.Bytes(buffer.getvalue()))


class VisibilityView(Gtk.Box):
    """The night-timeline view: plot + suggested queue order + apply/export.

    - ``queue_controller`` — the plan-queue controller (duck-typed: ``entries``
      / ``move`` / ``add_listener``; the app's shared
      :class:`~seestar_controller.observing.queue_controller.PlanQueueController`).
      A fresh one is created when omitted, so the widget also works standalone;
    - ``settings`` — the site settings source: a mapping (e.g.
      ``AppState.settings``) or a zero-arg callable returning one (the live
      settings of the app), read on every render via
      :meth:`~seestar_controller.visibility.Site.from_settings`. ``None`` means
      no site — the view shows the "set the observing site" placeholder;
    - ``night`` — the night date (a ``date``/``datetime``; ``None`` = tonight at
      the site's timezone, resolved on every render).
    """

    def __init__(
        self,
        queue_controller: Any = None,
        *,
        settings: SettingsSource | None = None,
        night: Any = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("orientation", Gtk.Orientation.VERTICAL)
        kwargs.setdefault("spacing", 8)
        super().__init__(**kwargs)
        if queue_controller is None:
            # lazy import: the observing package is pure, but keep the widget's
            # module import light (and free of the observing dependency unless
            # a standalone controller is actually needed)
            from seestar_controller.observing.queue_controller import (
                PlanQueueController,
            )

            queue_controller = PlanQueueController()
        self._queue_controller = queue_controller
        self._settings = settings
        self._night = night
        self._main_thread_id = threading.get_ident()
        self._model: TimelineModel | None = None
        self._night_used: datetime.date | None = None
        self._png_bytes: bytes = b""
        self._texture: Gdk.Texture | None = None
        self._surface: cairo.ImageSurface | None = None

        # header row: heading + night label + Refresh + Export PNG
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.append(Gtk.Label(label="Night timeline", xalign=0.0, css_classes=["heading"]))
        self._night_label = Gtk.Label(
            label="", xalign=0.0, hexpand=True, css_classes=["dim-label", "caption"]
        )
        header.append(self._night_label)
        self._refresh_button = Gtk.Button(
            label="Refresh", tooltip_text="Recompute the timeline for tonight (or the set night)"
        )
        self._refresh_button.connect("clicked", lambda *_args: self.refresh())
        header.append(self._refresh_button)
        self._export_button = Gtk.Button(
            label="Export PNG", tooltip_text="Save the timeline plot as a PNG file"
        )
        self._export_button.connect("clicked", self._on_export_clicked)
        header.append(self._export_button)
        self.append(header)

        # plot | placeholder
        self._stack = Gtk.Stack()
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)
        self._plot = Gtk.DrawingArea()
        self._plot.set_hexpand(True)
        self._plot.set_vexpand(True)
        self._plot.set_content_width(DEFAULT_WIDTH_PX)
        self._plot.set_content_height(DEFAULT_HEIGHT_PX)
        self._plot.set_draw_func(self._draw_plot)
        plot_frame = Gtk.Frame(child=self._plot)
        plot_frame.set_vexpand(True)
        self._stack.add_titled(plot_frame, "plot", "Plot")
        self._placeholder_label = Gtk.Label(
            label="The queue is empty — add targets above to see the night timeline",
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            wrap=True,
            css_classes=["dim-label"],
        )
        self._stack.add_titled(self._placeholder_label, "empty", "Empty")
        self._stack.set_visible_child_name("empty")
        self.append(self._stack)

        # suggested order list + Apply button
        group = Adw.PreferencesGroup(title="Suggested queue order")
        self._suggested_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._suggested_list.add_css_class("boxed-list")
        group.add(self._suggested_list)
        self.append(group)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.CENTER)
        self._apply_button = Gtk.Button(
            label="Apply suggested order",
            css_classes=["suggested-action"],
            tooltip_text="Reorder the plan queue into the suggested order (by best altitude window)",
        )
        self._apply_button.connect("clicked", self._on_apply_clicked)
        buttons.append(self._apply_button)
        self.append(buttons)

        self._status_label = Gtk.Label(label="", wrap=True, halign=Gtk.Align.CENTER)
        self.append(self._status_label)

        self._queue_controller.add_listener(self._on_queue_event)
        self._render()

    # -- public helpers (also used by the widget tests) ------------------------------

    @property
    def queue_controller(self) -> Any:
        """The plan-queue controller backing this view."""
        return self._queue_controller

    @property
    def controller(self) -> Any:
        """Alias of :attr:`queue_controller`."""
        return self._queue_controller

    @property
    def settings(self) -> SettingsSource | None:
        """The settings source (mapping or callable) the site is read from."""
        return self._settings

    @property
    def night(self) -> datetime.date | None:
        """The night date used by the last render (``None`` before the first
        successful render; the constructor's ``night`` when set)."""
        return self._night_used

    @property
    def plot(self) -> Gtk.DrawingArea:
        """The timeline drawing area."""
        return self._plot

    @property
    def stack(self) -> Gtk.Stack:
        """The plot/placeholder stack."""
        return self._stack

    @property
    def placeholder_label(self) -> Gtk.Label:
        """The empty-state placeholder label."""
        return self._placeholder_label

    @property
    def status_label(self) -> Gtk.Label:
        """The status label (summary line / clean error)."""
        return self._status_label

    @property
    def refresh_button(self) -> Gtk.Button:
        return self._refresh_button

    @property
    def export_button(self) -> Gtk.Button:
        return self._export_button

    @property
    def apply_button(self) -> Gtk.Button:
        """The 'Apply suggested order' button."""
        return self._apply_button

    @property
    def suggested_list(self) -> Gtk.ListBox:
        """The suggested-order list (one row per queued entry)."""
        return self._suggested_list

    @property
    def night_label(self) -> Gtk.Label:
        """The header's night label."""
        return self._night_label

    @property
    def texture(self) -> Gdk.Texture | None:
        """The current plot texture (PNG -> PIL -> ``Gdk.Texture``), or None."""
        return self._texture

    @property
    def png_bytes(self) -> bytes:
        """The current rendered PNG bytes (empty before the first render)."""
        return self._png_bytes

    @property
    def model(self) -> TimelineModel | None:
        """The current timeline model (None when the last render failed)."""
        return self._model

    def suggested_order(self) -> tuple[int, ...]:
        """The currently suggested queue order (indices; empty when no model)."""
        return self._model.order if self._model is not None else ()

    def suggested_row_labels(self) -> tuple[str, ...]:
        """The labels of the suggested-order list rows, in row order."""
        labels: list[str] = []
        index = 0
        while (row := self._suggested_list.get_row_at_index(index)) is not None:
            child = row.get_child()
            if isinstance(child, Gtk.Label):
                labels.append(child.get_label())
            index += 1
        return tuple(labels)

    def refresh(self) -> None:
        """Recompute the timeline (re-reads the settings + the queue)."""
        self._render()

    def export_png(self, path: str | Path) -> Path:
        """Write the current timeline PNG to ``path``; returns the
        :class:`~pathlib.Path`. Raises :class:`VisibilityError` when nothing
        has been rendered yet."""
        if not self._png_bytes:
            raise VisibilityError("nothing to export — the timeline has not been rendered yet")
        out = Path(path)
        out.write_bytes(self._png_bytes)
        return out

    # -- events ------------------------------------------------------------------------

    def _on_queue_event(self, event: Any) -> None:
        """Queue-changed listener: re-render (marshalled onto the main loop
        when the event comes from a worker thread)."""
        if threading.get_ident() != self._main_thread_id:
            GLib.idle_add(self._render)
            return
        self._render()

    def _on_apply_clicked(self, *_args) -> None:
        """Apply button: write the suggested order back to the PlanQueue via
        the queue controller's public ``move`` API."""
        model = self._model
        if model is None:
            self._set_status("no timeline to apply — render first", error=True)
            return
        try:
            moves = apply_order(self._queue_controller, model.order)
        except Exception as exc:  # noqa: BLE001 - shown inline, never crash the view
            self._set_status(f"{type(exc).__name__}: {exc}", error=True)
            return
        # the queue events re-rendered the view already; make sure of it, then
        # report (the render resets the status line, so set it afterwards)
        self._render()
        if moves == 0:
            self._set_status("queue already in the suggested order", error=False)
        else:
            self._set_status(f"applied suggested order ({moves} move{'s' if moves != 1 else ''})", error=False)

    def _on_export_clicked(self, *_args) -> None:
        """Export PNG button: a :class:`Gtk.FileDialog` save dialog; the chosen
        path is written via :meth:`export_png` in the async callback."""
        night = self._night_used
        dialog = Gtk.FileDialog(
            title="Export timeline PNG",
            accept_label="Export PNG",
        )
        dialog.set_initial_name(f"night-timeline-{night.isoformat() if night else 'tonight'}.png")
        dialog.save(
            self.get_root(),
            None,
            self._on_export_dialog_finished,
        )

    def _on_export_dialog_finished(self, dialog: Gtk.FileDialog, result: Any) -> None:
        """FileDialog save callback: write the PNG to the chosen file (inline
        error when there is nothing to export or the write fails)."""
        try:
            file = dialog.save_finish(result)
        except Exception:  # noqa: BLE001 - cancelled dialog
            return
        if file is None:
            return
        try:
            out = self.export_png(file.get_path())
        except Exception as exc:  # noqa: BLE001 - shown inline, never crash the view
            self._set_status(f"{type(exc).__name__}: {exc}", error=True)
            return
        self._set_status(f"exported {out}", error=False)

    # -- rendering ---------------------------------------------------------------------

    def _resolve_night(self, site: Site) -> datetime.date:
        """The night date: the constructor's ``night`` when set, else TODAY at
        the site's timezone."""
        if self._night is not None:
            if isinstance(self._night, datetime.datetime):
                return self._night.date()
            if isinstance(self._night, datetime.date):
                return self._night
            raise VisibilityError(f"night must be a date/datetime, got {self._night!r}")
        return datetime.datetime.now(site.tzinfo()).date()

    def _render(self) -> None:
        """Recompute the model, render the PNG -> PIL -> texture, and refresh
        the plot, the suggested-order list and the status line. Any failure
        (no site configured, bad settings, ...) lands in the status line and
        the placeholder — never a crash."""
        try:
            settings = resolve_settings(self._settings)
            if settings is None:
                raise VisibilityError(
                    "no settings source — the observing site is unknown "
                    "(set site_latitude/site_longitude in settings.json)"
                )
            site = Site.from_settings(settings)
            night = self._resolve_night(site)
            entries = self._queue_controller.entries
            if not entries:
                raise _EmptyQueue()
            model = build_timeline(entries, site, night)
            png = render_timeline_png(model)
            texture = texture_from_pil(Image.open(io.BytesIO(png)))
            surface = cairo.ImageSurface.create_from_png(io.BytesIO(png))
        except _EmptyQueue:
            self._show_placeholder(
                "The queue is empty — add targets above to see the night timeline",
                "queue is empty",
                error=False,
            )
            return
        except Exception as exc:  # noqa: BLE001 - shown inline, never crash the view
            self._show_placeholder(
                "Timeline unavailable", f"{type(exc).__name__}: {exc}", error=True
            )
            return

        try:
            self._model = model
            self._night_used = night
            self._png_bytes = png
            self._texture = texture
            self._surface = surface
            self._stack.set_visible_child_name("plot")
            self._night_label.set_label(f"night of {night.isoformat()} · {site.timezone}")
            self._rebuild_suggested_list(suggested_labels(model))
            identity = tuple(range(len(model.order)))
            self._apply_button.set_sensitive(model.order != identity)
            self._set_status(self._summary(model), error=False)
            self._plot.queue_draw()
        except Exception as exc:  # noqa: BLE001 - shown inline, never crash the view
            self._show_placeholder(
                "Timeline unavailable", f"{type(exc).__name__}: {exc}", error=True
            )

    def _show_placeholder(self, placeholder_text: str, status_text: str, *, error: bool) -> None:
        """Reset the render state and show the placeholder page + status."""
        self._model = None
        self._night_used = None
        self._png_bytes = b""
        self._texture = None
        self._surface = None
        self._stack.set_visible_child_name("empty")
        self._placeholder_label.set_label(placeholder_text)
        self._rebuild_suggested_list(())
        self._apply_button.set_sensitive(False)
        self._night_label.set_label("")
        self._set_status(status_text, error=error)
        self._plot.queue_draw()

    def _summary(self, model: TimelineModel) -> str:
        """The one-line status summary of a rendered model."""
        twilight = model.twilight
        if twilight.astro_start is not None and twilight.astro_end is not None:
            twilight_text = (
                f"astro dusk {format_local(model.site, twilight.astro_start)} · "
                f"dawn {format_local(model.site, twilight.astro_end)}"
            )
        else:
            twilight_text = "no astronomical twilight (polar day/night)"
        alt = model.moon.alt.to_value(u.deg)
        phase = model.moon.phase.to_value(u.dimensionless_unscaled)
        moon_text = f"moon {float(phase[int(np.argmax(alt))]) * 100.0:.0f}%" if alt.size else "moon —"
        return f"{len(model.curves)} target(s) · {twilight_text} · {moon_text}"

    def _rebuild_suggested_list(self, labels: tuple[str, ...]) -> None:
        """Rebuild the suggested-order list rows: ``"1. name"`` per entry."""
        while (row := self._suggested_list.get_row_at_index(0)) is not None:
            self._suggested_list.remove(row)
        for i, label in enumerate(labels):
            self._suggested_list.append(
                Gtk.Label(
                    label=f"{i + 1}. {label}",
                    halign=Gtk.Align.START,
                    margin_top=4,
                    margin_bottom=4,
                    margin_start=8,
                    margin_end=8,
                )
            )

    def _draw_plot(self, area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        """DrawingArea draw func: paint the rendered timeline texture (PNG ->
        PIL -> Gdk.Texture, cached as a cairo surface) scaled to the widget's
        allocation, centered, aspect preserved."""
        surface = self._surface
        if surface is None:
            cr.set_source_rgba(0.5, 0.5, 0.5, 1.0)
            cr.set_font_size(12.0)
            cr.move_to(12.0, height / 2.0)
            cr.show_text("no timeline rendered")
            return
        sw = surface.get_width()
        sh = surface.get_height()
        if sw <= 0 or sh <= 0 or width <= 0 or height <= 0:
            return
        scale = min(width / sw, height / sh)
        cr.translate((width - sw * scale) / 2.0, (height - sh * scale) / 2.0)
        cr.scale(scale, scale)
        cr.set_source_surface(surface, 0.0, 0.0)
        cr.paint()

    def _set_status(self, text: str, *, error: bool) -> None:
        self._status_label.set_label(text)
        for css_class in ("error", "dim-label"):
            self._status_label.remove_css_class(css_class)
        if error:
            self._status_label.add_css_class("error")
        else:
            self._status_label.add_css_class("dim-label")


class _EmptyQueue(Exception):
    """Internal control-flow exception: the queue is empty (placeholder state,
    not an error)."""
