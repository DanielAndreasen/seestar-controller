"""Image enhance — open a project's stacks and render them tone-mapped in the
Enhance view (``.scratch/image-enhance/spec.md``).

Strict two-layer split (the sandbox has no GTK, see docs/wiki/assumptions.md):

- **Pure layer** — :mod:`.fits_io` (:func:`~.fits_io.load_fits` ->
  :class:`~.fits_io.FitsImage` with astropy, BZERO/BSCALE/BLANK handled
  manually and NaN-safe; :func:`~.fits_io.list_project_stacks` ->
  :class:`~.fits_io.StackInfo` via the store's frames/sessions;
  :class:`~.fits_io.EnhanceError`), :mod:`.render` (:func:`~.render.tonemap`
  percentile tone mapping with linear/asinh/log/sqrt stretches + gamma,
  :func:`~.render.to_pil_image`, :func:`~.render.downscale`,
  :func:`~.render.render_preview` — downscale first, then tone-map, so slider
  interaction stays smooth), :mod:`.viewer_controller`
  (:class:`~.viewer_controller.ViewerController` — the app-level shared
  controller: current project, stack list, open_stack with a path+mtime FITS
  cache, frozen :class:`~.viewer_controller.ViewerSnapshot` listener
  notifications), and — ticket 02 — :mod:`.ops` + :mod:`.pipeline` (the
  composable pure enhancement ops: :func:`~.ops.apply_stretch`,
  :func:`~.ops.apply_denoise`, :func:`~.ops.apply_sharpen`,
  :func:`~.ops.apply_white_balance`, :func:`~.ops.apply_background`, each with
  a validating frozen params dataclass, plus :class:`~.pipeline.Op`,
  :class:`~.pipeline.Pipeline` (ordered, re-runnable, immutable
  :meth:`~.pipeline.Pipeline.with_param`), :func:`~.pipeline.default_pipeline`,
  :func:`~.pipeline.render_pipeline`). All import **nothing** from ``gi`` —
  stdlib + numpy/PIL + store only — fully unit-testable headless; a
  subprocess test enforces the boundary.
- **Widget layer** — :mod:`.viewer` (:class:`~.viewer.ImageViewer`, the
  stack list + tone-mapped Gtk.Picture). It imports ``gi`` at module top and
  is re-exported here **lazily** (PEP 562 ``__getattr__``), so
  ``import seestar_controller.enhance`` never imports ``gi``.

On the desktop, PyGObject is not a pip dependency — install the system
packages ``python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`` (see the README).

Ticket 01 covers loading + viewing; ticket 02 adds the pure enhancement
pipeline (:mod:`.ops` + :mod:`.pipeline`), which will hang off
:class:`~.viewer_controller.ViewerController` in the later view tickets.
"""

from .fits_io import (
    EnhanceError,
    FitsImage,
    StackInfo,
    list_project_stacks,
    load_fits,
)
from .ops import (
    BACKGROUND_METHODS,
    DEFAULT_HISTOGRAM_BINS,
    OP_NAMES,
    OPS,
    STRETCH_KINDS,
    WB_MODES,
    BackgroundParams,
    DenoiseParams,
    OpSpec,
    SharpenParams,
    StretchParams,
    WhiteBalanceParams,
    apply_background,
    apply_denoise,
    apply_sharpen,
    apply_stretch,
    apply_white_balance,
)
from .pipeline import (
    Op,
    Pipeline,
    default_pipeline,
    render_pipeline,
)
from .render import (
    ASINH_SOFTNESS,
    DEFAULT_HI_PERCENTILE,
    DEFAULT_LO_PERCENTILE,
    DEFAULT_MAX_PREVIEW_SIZE,
    DEFAULT_STRETCH,
    LOG_BASE,
    STRETCHES,
    downscale,
    render_preview,
    to_pil_image,
    tonemap,
)
from .viewer_controller import (
    MAX_CACHE_ENTRIES,
    ViewerController,
    ViewerListener,
    ViewerSnapshot,
)

#: Widget-layer names re-exported lazily (importing the module pulls in
#: ``gi`` — only the GUI path does).
_WIDGET_EXPORTS = {
    "ImageViewer": "viewer",
}


def __getattr__(name: str):
    if name in _WIDGET_EXPORTS:
        from importlib import import_module

        module = import_module(f".{_WIDGET_EXPORTS[name]}", __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ASINH_SOFTNESS",
    "BACKGROUND_METHODS",
    "DEFAULT_HISTOGRAM_BINS",
    "DEFAULT_HI_PERCENTILE",
    "DEFAULT_LO_PERCENTILE",
    "DEFAULT_MAX_PREVIEW_SIZE",
    "DEFAULT_STRETCH",
    "LOG_BASE",
    "MAX_CACHE_ENTRIES",
    "OP_NAMES",
    "OPS",
    "STRETCH_KINDS",
    "STRETCHES",
    "WB_MODES",
    "BackgroundParams",
    "DenoiseParams",
    "EnhanceError",
    "FitsImage",
    "Op",
    "OpSpec",
    "Pipeline",
    "SharpenParams",
    "StackInfo",
    "StretchParams",
    "ViewerController",
    "ViewerListener",
    "ViewerSnapshot",
    "WhiteBalanceParams",
    "apply_background",
    "apply_denoise",
    "apply_sharpen",
    "apply_stretch",
    "apply_white_balance",
    "default_pipeline",
    "downscale",
    "list_project_stacks",
    "load_fits",
    "render_pipeline",
    "render_preview",
    "to_pil_image",
    "tonemap",
    # widget layer (lazy, imports gi):
    "ImageViewer",
]
