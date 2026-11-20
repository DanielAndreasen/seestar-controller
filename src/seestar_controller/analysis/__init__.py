"""Analysis — period analysis of a project's lightcurve (the
``.scratch/period-analysis/spec.md`` package: "analysis pure Python (series
→ periodogram/fold); view renders").

Strict two-layer split (the sandbox has no GTK, see docs/wiki/assumptions.md):

- **Pure layer** — :mod:`.lightcurve` (period-analysis ticket 01, the
  lightcurve view's controller + plot transform math): stdlib +
  :mod:`seestar_controller.store` only — :class:`~.lightcurve.LightcurveController`
  (the app-level shared controller following the shared viewer controller's
  project: frozen :class:`~.lightcurve.LightcurveSnapshot` with the jd-ordered
  :class:`~.lightcurve.LightcurvePoint`\\ s of ALL sessions incl. excluded
  points, the first-seen ``sessions`` order, and a never-raising ``error``;
  ``refresh()`` / ``set_project()`` / ``attach_viewer()``; listeners with
  frozen snapshots marshalled via ``invoke_on_main``), the deterministic
  session color assignment (:data:`~.lightcurve.SESSION_PALETTE` +
  :func:`~.lightcurve.session_color`), and the pure plot transform math
  (:class:`~.lightcurve.PlotGeometry` / :class:`~.lightcurve.PlotWindow` /
  :func:`~.lightcurve.full_window` / :func:`~.lightcurve.data_to_screen` /
  :func:`~.lightcurve.screen_to_data` / :func:`~.lightcurve.zoom_window_at` /
  :func:`~.lightcurve.pan_window` — inverted mag axis, zoom-at-cursor, pan).
  It imports **nothing** from ``gi``, ``link``, ``ui``, or ``photometry`` —
  fully unit-testable headless; a subprocess test enforces the boundary.
- **Widget layer** — :mod:`.lightcurve_view` (:class:`~.lightcurve_view.LightcurveView`,
  the Lightcurve view's plot: a :class:`Gtk.DrawingArea` painting mag vs JD
  with one color per session + legend, error bars per point, excluded points
  marked red, the inverted mag axis, and zoom (scroll around the cursor) /
  pan (drag) / double-click reset driven by the pure transform math). It
  imports ``gi`` at module top and is re-exported here **lazily** (PEP 562
  ``__getattr__``), so ``import seestar_controller.analysis`` never imports
  ``gi``.

On the desktop, PyGObject is not a pip dependency — install the system
packages ``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` (see the README).

Ticket 01 covers the lightcurve view; later tickets add the Lomb-Scargle
periodogram, period selection + phase fold, and CSV export / outlier
management.
"""

from .lightcurve import (
    AXIS_COLOR,
    DB_CALL_TIMEOUT_S,
    DEFAULT_WINDOW,
    EXCLUDED_COLOR,
    MIN_JD_SPAN,
    MIN_MAG_SPAN,
    PLOT_MARGIN_BOTTOM,
    PLOT_MARGIN_LEFT,
    PLOT_MARGIN_RIGHT,
    PLOT_MARGIN_TOP,
    SESSION_PALETTE,
    ZOOM_FACTOR,
    AnalysisError,
    InvokeOnMain,
    LightcurveController,
    LightcurveError,
    LightcurveListener,
    LightcurvePoint,
    LightcurveSnapshot,
    PlotGeometry,
    PlotWindow,
    data_to_screen,
    full_window,
    pan_window,
    screen_to_data,
    session_color,
    session_color_index,
    zoom_window_at,
)

#: Widget-layer names re-exported lazily (importing the module pulls in
#: ``gi`` — only the GUI path does).
_WIDGET_EXPORTS = {
    "LightcurveView": "lightcurve_view",
}


def __getattr__(name: str):
    if name in _WIDGET_EXPORTS:
        from importlib import import_module

        module = import_module(f".{_WIDGET_EXPORTS[name]}", __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AXIS_COLOR",
    "DB_CALL_TIMEOUT_S",
    "DEFAULT_WINDOW",
    "EXCLUDED_COLOR",
    "MIN_JD_SPAN",
    "MIN_MAG_SPAN",
    "PLOT_MARGIN_BOTTOM",
    "PLOT_MARGIN_LEFT",
    "PLOT_MARGIN_RIGHT",
    "PLOT_MARGIN_TOP",
    "SESSION_PALETTE",
    "ZOOM_FACTOR",
    "AnalysisError",
    "InvokeOnMain",
    "LightcurveController",
    "LightcurveError",
    "LightcurveListener",
    "LightcurvePoint",
    "LightcurveSnapshot",
    "PlotGeometry",
    "PlotWindow",
    "data_to_screen",
    "full_window",
    "pan_window",
    "screen_to_data",
    "session_color",
    "session_color_index",
    "zoom_window_at",
    # widget layer (lazy, imports gi):
    "LightcurveView",
]
