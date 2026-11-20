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
- **Pure layer** — :mod:`.periodogram` (period-analysis ticket 02, the
  Lomb-Scargle core): :mod:`numpy` + :mod:`astropy.timeseries` +
  :mod:`seestar_controller.store` only — :func:`~.periodogram.frequency_grid`
  (the frequency grid from the baseline and cadence: ``f_min = min_cycles /
  baseline``, ``f_max = nyquist_factor * 0.5 / median_dt``, uniform ``df =
  1 / (oversample * baseline)``), :func:`~.periodogram.select_peaks`
  (local maxima, deduplicated to >= 1/baseline in frequency, top-N by power),
  :func:`~.periodogram.compute_periodogram` (points in → frozen
  :class:`~.periodogram.PeriodogramResult` with the grid info, the full
  spectrum, the top-N :class:`~.periodogram.Peak`\\ s and the top peak's
  Baluev false-alarm probability out; excluded points filtered by default),
  :func:`~.periodogram.project_periodogram` (the store-backed wrapper reading
  the project's lightcurve), and :class:`~.periodogram.PeriodogramError` under
  :class:`~.lightcurve.AnalysisError`. Also imports **nothing** from ``gi``,
  ``link``, ``ui``, or ``photometry`` — subprocess-enforced.
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

Ticket 01 covers the lightcurve view; ticket 02 adds the Lomb-Scargle
periodogram core; later tickets add period selection + phase fold, and CSV
export / outlier management.
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
from .periodogram import (
    ALLOWED_NORMALIZATIONS,
    DEFAULT_MIN_CYCLES,
    DEFAULT_NORMALIZATION,
    DEFAULT_NYQUIST_FACTOR,
    DEFAULT_OVERSAMPLE,
    DEFAULT_TOP_N,
    MAX_GRID_POINTS,
    Peak,
    PeriodogramError,
    PeriodogramResult,
    compute_periodogram,
    frequency_grid,
    project_periodogram,
    select_peaks,
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
    "ALLOWED_NORMALIZATIONS",
    "AXIS_COLOR",
    "DB_CALL_TIMEOUT_S",
    "DEFAULT_MIN_CYCLES",
    "DEFAULT_NORMALIZATION",
    "DEFAULT_NYQUIST_FACTOR",
    "DEFAULT_OVERSAMPLE",
    "DEFAULT_TOP_N",
    "DEFAULT_WINDOW",
    "EXCLUDED_COLOR",
    "MAX_GRID_POINTS",
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
    "Peak",
    "PeriodogramError",
    "PeriodogramResult",
    "PlotGeometry",
    "PlotWindow",
    "compute_periodogram",
    "data_to_screen",
    "frequency_grid",
    "full_window",
    "pan_window",
    "project_periodogram",
    "screen_to_data",
    "select_peaks",
    "session_color",
    "session_color_index",
    "zoom_window_at",
    # widget layer (lazy, imports gi):
    "LightcurveView",
]
