# 02: Night timeline view + apply suggested order

**What to build:** the GUI for the visibility planner: a night timeline plot (altitude curves for all queued targets, twilight bands, moon track — matplotlib Agg -> PNG -> PIL texture, same path as the analysis plots), PNG export, and the suggested queue order shown as a hint with an "Apply" button that reorders the PlanQueue. Demoable: with a queued plan, the timeline renders in the GUI with twilight/moon marked; Apply reorders the queue in the Observe view.

**Blocked by:** visibility-planner 01

**Status:** implemented on branch `wt/visibility-02-timeline` (single ticket commit; hash reported with the ticket)

- [x] Widget `VisibilityView` following analysis/lightcurve_view.py patterns (DrawingArea + Agg render): altitude curves per queued target, twilight shading, moon altitude/phase annotation
- [x] Suggested order list + "Apply" button writing the reordered entries back to the observing PlanQueue (queue_controller)
- [x] PNG export of the timeline
- [x] Wired into the app (Observe view or its own tab — decide with existing patterns)
- [x] Widget tests via the ui harness; full suite green

## Comments

Implemented as a strict pure/widget split inside `visibility/`:

- pure `timeline.py` — `TimelineModel` + `build_timeline(entries, site, night)` (window, 0–24 h axis, one `AltitudeCurve` per resolved entry with its queue index, twilight, moon, suggested order, labels), `reorder_moves` (pop/insert move sequence realizing a permutation — exactly `PlanQueue.move` semantics), `apply_order(queue, order)` (duck-typed `len()`/`move()`, writes the suggestion back through the queue controller's public API only), `suggested_labels`, `axis_hours`, `format_local`, `resolve_settings` (mapping or live callable settings source);
- pure `render.py` — `render_timeline_png(model)`: matplotlib `Figure` + `FigureCanvasAgg` (no pyplot) → PNG bytes; twilight `axvspan` with dusk/dawn annotations, horizon line, per-target curves with legend, dashed moon track labeled with its illuminated fraction at culmination, local-time x ticks every 2 h;
- widget `timeline_view.py` — `VisibilityView(Gtk.Box)`, `import gi` at module top, lazy PEP 562 export (`visibility.VisibilityView`): header (heading, night label, Refresh, Export PNG via `Gtk.FileDialog`), `Gtk.Stack` plot page (`Gtk.DrawingArea` painting the PNG→PIL→`Gdk.Texture` render as a cached cairo surface, scaled/aspect-preserved) | empty placeholder, suggested-order `Gtk.ListBox` + "Apply suggested order" button, status line. Re-renders on every queue event (worker-thread events marshalled via `GLib.idle_add`) and on Refresh; empty queue / missing site show the placeholder with the reason inline (never a crash). `export_png(path)` is the programmatic export (also driven by the dialog callback).
- wiring: the Observe PAGE (`ui/views.py::ObserveView`) appends the `VisibilityView` below the observe widget, fed by the shared `queue_controller` and the app state's live settings (`build_view(..., app_state=...)`; `SeestarWindow` passes `app_state=state`). No observing/ or store files touched — the PlanQueue is read/written only via the queue controller's public API.
- `matplotlib` added as a project dependency (`uv add matplotlib`; only pygobject is forbidden); `timeline_view.py` + `tests/test_visibility_widgets.py` added to the pyproject ty-override list; the package audit in `tests/test_visibility_planner.py` now classifies pure (`__init__`, `site`, `planner`, `timeline`, `render`) vs widget (`timeline_view`, `import gi` before `from gi.repository`, `_WIDGET_EXPORTS == {"VisibilityView": "timeline_view"}`) and the subprocess boundary test also renders a PNG offline.
- tests: `tests/test_visibility_timeline.py` (pure: model contents, validation, reorder_moves simulated against a list, apply_order on a real `PlanQueueController` incl. `moved` events + idempotence, PNG size/validation/edge cases) and `tests/test_visibility_widgets.py` (ui harness: render/texture/model/suggested rows, Apply reorders the queue and disables itself, queue changes re-render, empty/missing-site placeholders, live-settings refresh, export_png, `build_view("observe", ...)` + `SeestarWindow` wiring).

