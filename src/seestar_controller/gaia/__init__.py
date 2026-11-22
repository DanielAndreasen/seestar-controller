"""Gaia DR3 cross-match of the sources detected on a session STACK — pure core.

Pure-Python package (module boundary per ``.scratch/gaia-crossmatch/spec.md``):
**no GTK imports, no :mod:`seestar_controller.link` imports** — a subprocess
test enforces the boundary. Nothing here imports the widget layer or the
enhance pipeline; the only seestar imports are the pure
:mod:`seestar_controller.catalog` (coordinate math + the
:class:`~seestar_controller.catalog.cache.QueryCache` cache pattern, reused
read-only), :func:`seestar_controller.store.frames.header_to_dict`, and —
for the issue-02 persistence glue only — the pure
:mod:`seestar_controller.store` (which itself imports neither GTK nor link).

Pipeline (issue 01 of gaia-crossmatch — detection + query + cache; storing
the table into the project is issue 02):

1. :func:`~.detect.detect_sources` — photutils ``DAOStarFinder`` over the
   stack (FWHM estimated from the image, configurable threshold) with the
   WCS read from the stack FITS header; a header without a celestial WCS
   raises :class:`~.errors.WcsMissingError` ("plate-solve the stack first");
2. :func:`~.query.query_gaia_cached` — a Gaia DR3 cone search around the
   field center (radius = field diagonal + margin) via direct VizieR TAP
   HTTP, selecting G/BP/RP/parallax(+error)/pmRA(+error)/pmDE(+error) with
   ``G < 20``, cached on disk under ``<config>/cache/gaia/`` keyed by the
   rounded center + radius with a TTL (the catalog module's cache pattern);
   a query failure with no cache raises
   :class:`~.errors.GaiaUnavailableError`;
3. :func:`~.matching.crossmatch` — pair every detection with its nearest
   Gaia row within a configurable radius (default 1.5 arcsec); unmatched
   detections are flagged.

:func:`~.matching.crossmatch_stack` runs the whole pipeline over one
stack. The single HTTP boundary is :func:`~.query.fetch_gaia_tap_json` —
tests mock it, so no test ever touches the network.

Issue 02 adds the persistence glue (:mod:`.persistence`): the cross-match
table of a stack is stored in the project db (``crossmatch`` table, schema
v6) via the pure :mod:`seestar_controller.store`, so repeat views are instant
and offline-capable — :func:`~.persistence.save_crossmatch`,
:func:`~.persistence.load_crossmatch`,
:func:`~.persistence.list_crossmatched_stacks`.

Modules:

- :mod:`.errors` — the error taxonomy (:class:`~.errors.GaiaError` base,
  :class:`~.errors.DetectionError`, :class:`~.errors.WcsMissingError`,
  :class:`~.errors.GaiaUnavailableError`, :class:`~.errors.GaiaResponseError`);
- :mod:`.detect` — :class:`~.detect.DetectedSource`,
  :func:`~.detect.detect_sources`, :func:`~.detect.estimate_fwhm`,
  :func:`~.detect.pixel_to_sky`, :func:`~.detect.field_center_and_radius`;
- :mod:`.query` — :class:`~.query.GaiaStar`, :func:`~.query.build_cone_query`,
  :func:`~.query.fetch_gaia_tap_json`, :func:`~.query.parse_gaia_response`,
  :func:`~.query.query_gaia`, :func:`~.query.query_gaia_cached`,
  :func:`~.query.gaia_cache_key`, :func:`~.query.default_cache_dir`;
- :mod:`.matching` — :class:`~.matching.CrossmatchRow`,
  :func:`~.matching.crossmatch`, :func:`~.matching.crossmatch_stack`;
- :mod:`.persistence` — the project-store glue of issue 02:
  :func:`~.persistence.save_crossmatch`, :func:`~.persistence.load_crossmatch`,
  :func:`~.persistence.list_crossmatched_stacks`.

Issue 03 adds the overlay layer:

- :mod:`.overlay` — the PURE render + controller layer:
  :func:`~.overlay.tonemap_gray` / :func:`~.overlay.render_stack_overlay`
  draw the cross-match on the tone-mapped stack (matched detections ringed
  and labelled with their Gaia G magnitude, unmatched detections flagged
  with a distinct X marker), and :class:`~.overlay.CrossmatchController` is
  the app-level shared controller (following the shared viewer controller's
  open stack) that runs the pipeline with the configurable match radius +
  G limit (cache-first re-query on change), persists the table, and renders
  the annotated overlay;
- :mod:`.overlay_view` — the WIDGET layer (imports ``gi`` at module top,
  re-exported lazily below): :class:`~.overlay_view.CrossmatchOverlayView`,
  the cross-match panel of the image viewer.
"""

from .detect import (
    DEFAULT_FWHM_PX,
    DEFAULT_MARGIN_DEG,
    DEFAULT_THRESHOLD_SIGMA,
    MAX_FWHM_PX,
    MIN_FWHM_PX,
    DetectedSource,
    detect_sources,
    estimate_fwhm,
    field_center_and_radius,
    pixel_to_sky,
)
from .errors import (
    DetectionError,
    GaiaError,
    GaiaResponseError,
    GaiaUnavailableError,
    WcsMissingError,
)
from .matching import (
    DEFAULT_MATCH_RADIUS_ARCSEC,
    CrossmatchRow,
    crossmatch,
    crossmatch_stack,
)
from .overlay import (
    DB_CALL_TIMEOUT_S,
    DEFAULT_HI_PERCENTILE,
    DEFAULT_LO_PERCENTILE,
    DEFAULT_MAX_OVERLAY_SIZE,
    LINE_WIDTH,
    MATCHED_COLOR,
    MAX_G_LIMIT,
    MAX_MATCH_RADIUS_ARCSEC,
    MIN_G_LIMIT,
    MIN_MATCH_RADIUS_ARCSEC,
    POINT_RADIUS,
    RING_RADIUS,
    UNMATCHED_COLOR,
    CrossmatchController,
    CrossmatchListener,
    CrossmatchSnapshot,
    InvokeOnMain,
    render_stack_overlay,
    tonemap_gray,
)
from .persistence import (
    list_crossmatched_stacks,
    load_crossmatch,
    save_crossmatch,
)
from .query import (
    ARCSEC_PER_DEG,
    CACHE_DIR_ENV_VAR,
    DEFAULT_CACHE_DIR,
    DEFAULT_G_LIMIT,
    DEFAULT_MAXREC,
    DEFAULT_TIMEOUT_S,
    DEFAULT_TTL_S,
    GAIA_COLUMNS,
    GAIA_DR3_TABLE,
    KEY_ROUND_DIGITS,
    MAX_RADIUS_DEG,
    VIZIER_TAP_SYNC_URL,
    GaiaStar,
    build_cone_query,
    default_cache_dir,
    fetch_gaia_tap_json,
    gaia_cache_key,
    parse_gaia_response,
    query_gaia,
    query_gaia_cached,
)

#: Widget-layer exports, re-exported LAZILY (PEP 562): the widget modules
#: import ``gi`` at module top, so they must never be imported eagerly here —
#: only the attribute lookup triggers the import (and needs GTK, see
#: docs/wiki/assumptions.md).
_WIDGET_EXPORTS = {
    "CrossmatchOverlayView": "overlay_view",
}


def __getattr__(name: str):
    if name in _WIDGET_EXPORTS:
        from importlib import import_module

        module = import_module(f".{_WIDGET_EXPORTS[name]}", __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ARCSEC_PER_DEG",
    "CACHE_DIR_ENV_VAR",
    "DB_CALL_TIMEOUT_S",
    "DEFAULT_CACHE_DIR",
    "DEFAULT_FWHM_PX",
    "DEFAULT_G_LIMIT",
    "DEFAULT_HI_PERCENTILE",
    "DEFAULT_LO_PERCENTILE",
    "DEFAULT_MARGIN_DEG",
    "DEFAULT_MATCH_RADIUS_ARCSEC",
    "DEFAULT_MAXREC",
    "DEFAULT_MAX_OVERLAY_SIZE",
    "DEFAULT_THRESHOLD_SIGMA",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_TTL_S",
    "GAIA_COLUMNS",
    "GAIA_DR3_TABLE",
    "KEY_ROUND_DIGITS",
    "LINE_WIDTH",
    "MATCHED_COLOR",
    "MAX_FWHM_PX",
    "MAX_G_LIMIT",
    "MAX_MATCH_RADIUS_ARCSEC",
    "MAX_RADIUS_DEG",
    "MIN_FWHM_PX",
    "MIN_G_LIMIT",
    "MIN_MATCH_RADIUS_ARCSEC",
    "POINT_RADIUS",
    "RING_RADIUS",
    "UNMATCHED_COLOR",
    "VIZIER_TAP_SYNC_URL",
    "CrossmatchController",
    "CrossmatchListener",
    "CrossmatchOverlayView",
    "CrossmatchRow",
    "CrossmatchSnapshot",
    "DetectedSource",
    "DetectionError",
    "GaiaError",
    "GaiaResponseError",
    "GaiaStar",
    "GaiaUnavailableError",
    "InvokeOnMain",
    "WcsMissingError",
    "build_cone_query",
    "crossmatch",
    "crossmatch_stack",
    "default_cache_dir",
    "detect_sources",
    "estimate_fwhm",
    "fetch_gaia_tap_json",
    "field_center_and_radius",
    "gaia_cache_key",
    "list_crossmatched_stacks",
    "load_crossmatch",
    "parse_gaia_response",
    "pixel_to_sky",
    "query_gaia",
    "query_gaia_cached",
    "render_stack_overlay",
    "save_crossmatch",
    "tonemap_gray",
]
