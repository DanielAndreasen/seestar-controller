"""hr-diagram — CMD / H-R diagrams of a stack's Gaia cross-match table.

Spec: ``.scratch/hr-diagram/spec.md``. This package computes (a) the apparent
CMD points (G vs BP-RP), (b) the H-R points (absolute G =
``G + 5 - 5*log10(1000/parallax_mas)`` vs BP-RP, with the parallax SNR cut
``parallax/parallax_error > 5``), and (c) the cluster membership by
sigma-clipping parallax + proper motion around the median, plus the CSV
export of the table — from the gaia-crossmatch table of ONE stack (the
persisted :class:`~seestar_controller.store.crossmatch.CrossmatchRecord`
rows from :func:`~seestar_controller.store.crossmatch.load_crossmatch`,
consumed duck-typed by the pure modules — they import nothing from
:mod:`seestar_controller.store`).

**No ``gi`` import anywhere in the pure layer** — fully unit-testable
headless (the sandbox has no GTK, see docs/wiki/assumptions.md; a subprocess
test enforces the boundary). No network either (the isochrone fetch is
injectable and falls back to the bundled grid).

Modules:

- :mod:`.core` — ticket 01: :class:`~.core.HrError`, the frozen point record
  :class:`~.core.HrPoint` (``source_id``, ``g``, ``bp_rp``, ``abs_g``, the
  ``member`` flag, astrometry + errors), :func:`~.core.absolute_g`,
  :func:`~.core.membership_filter` (the frozen :class:`~.core.Membership`),
  :func:`~.core.cmd_points` and :func:`~.core.hr_points`;
- :mod:`.export` — :func:`~.export.export_table_csv` (the whole table as a
  CSV, atomic write, ``repr(float)`` round-trip floats) with
  :class:`~.export.ExportError` and :data:`~.export.CSV_HEADER`;
- :mod:`.isochrone` — ticket 02: the PARSEC isochrone overlay,
  :func:`~.isochrone.isochrone` (``age``, ``feh``, ``dist_mod`` -> overlay
  points, abs G vs BP-RP) with the disk cache under the app config dir, the
  single HTTP boundary :func:`~.isochrone.fetch_isochrone_json` (fixed
  :data:`~.isochrone.PARSEC_ISOCHRONE_URL`), and the bundled fallback grid
  (:func:`~.isochrone.fallback_isochrone`);
- :mod:`.plot` — ticket 03 (pure): the diagram transform math
  (:class:`~.plot.DiagramGeometry` / :class:`~.plot.DiagramWindow` /
  :func:`~.plot.data_to_screen` / :func:`~.plot.zoom_window_at` /
  :func:`~.plot.pan_window` / :func:`~.plot.point_at`), the matplotlib-Agg
  render :func:`~.plot.render_diagram_png` (PNG bytes, no display), and the
  stack-image marker render :func:`~.plot.render_stack_png`;
- :mod:`.controller` — ticket 03 (the store-boundary module, like
  :mod:`seestar_controller.gaia.overlay`): :class:`~.controller.HrDiagramSnapshot`
  and :class:`~.controller.HrDiagramController` — the app-level shared
  controller reading the cross-match table + isochrone params + stack image
  from the store (via ``_call_store`` marshalling) and computing the
  CMD/H-R points + overlay;
- :mod:`.view` — ticket 03 (the WIDGET module): :class:`~.view.HrDiagramView`,
  exported LAZILY (PEP 562, see :data:`_WIDGET_EXPORTS`) — it imports ``gi``
  at module top, so ``import seestar_controller.hr_diagram`` never imports
  GTK.
"""

from .controller import (
    DB_CALL_TIMEOUT_S,
    HrDiagramController,
    HrDiagramListener,
    HrDiagramSnapshot,
    load_stack_image,
    tonemap_gray,
)
from .core import (
    DEFAULT_PARALLAX_SNR,
    DEFAULT_SIGMA_CLIP,
    MAD_TO_SIGMA,
    MIN_MEMBERSHIP_SAMPLE,
    HrError,
    HrPoint,
    Membership,
    absolute_g,
    cmd_points,
    hr_points,
    membership_filter,
)
from .export import (
    CSV_HEADER,
    ExportError,
    export_table_csv,
)
from .isochrone import (
    PARSEC_ISOCHRONE_URL,
    IsochroneError,
    IsochroneFetchError,
    IsochronePoint,
    fallback_isochrone,
    fetch_isochrone_json,
    isochrone,
)
from .plot import (
    CMD,
    HR,
    KINDS,
    DiagramGeometry,
    DiagramModel,
    DiagramWindow,
    abs_g_error,
    data_to_screen,
    full_window,
    nearest_point_by_pixel,
    pan_window,
    point_at,
    point_xy,
    render_diagram_png,
    render_stack_png,
    screen_to_data,
    stack_image_scale,
    zoom_window_at,
)

#: The widget exports, mapped to their module — resolved LAZILY via the
#: module-level ``__getattr__`` (PEP 562), so importing this package never
#: imports ``gi`` (the widget module imports it at its top).
_WIDGET_EXPORTS = {
    "HrDiagramView": "view",
}


def __getattr__(name: str):
    if name in _WIDGET_EXPORTS:
        from importlib import import_module

        module = import_module(f".{_WIDGET_EXPORTS[name]}", __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CMD",
    "CSV_HEADER",
    "DB_CALL_TIMEOUT_S",
    "DEFAULT_PARALLAX_SNR",
    "DEFAULT_SIGMA_CLIP",
    "HR",
    "KINDS",
    "MAD_TO_SIGMA",
    "MIN_MEMBERSHIP_SAMPLE",
    "PARSEC_ISOCHRONE_URL",
    "DiagramGeometry",
    "DiagramModel",
    "DiagramWindow",
    "ExportError",
    "HrDiagramController",
    "HrDiagramListener",
    "HrDiagramSnapshot",
    "HrDiagramView",
    "HrError",
    "HrPoint",
    "IsochroneError",
    "IsochroneFetchError",
    "IsochronePoint",
    "Membership",
    "abs_g_error",
    "absolute_g",
    "cmd_points",
    "data_to_screen",
    "export_table_csv",
    "fallback_isochrone",
    "fetch_isochrone_json",
    "full_window",
    "hr_points",
    "isochrone",
    "load_stack_image",
    "membership_filter",
    "nearest_point_by_pixel",
    "pan_window",
    "point_at",
    "point_xy",
    "render_diagram_png",
    "render_stack_png",
    "screen_to_data",
    "stack_image_scale",
    "tonemap_gray",
    "zoom_window_at",
]
