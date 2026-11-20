"""Gaia DR3 cross-match of the sources detected on a session STACK — pure core.

Pure-Python package (module boundary per ``.scratch/gaia-crossmatch/spec.md``):
**no GTK imports, no :mod:`seestar_controller.link` imports** — a subprocess
test enforces the boundary. Nothing here imports the widget layer, the store
schema, or the enhance pipeline; the only seestar imports are the pure
:mod:`seestar_controller.catalog` (coordinate math + the
:class:`~seestar_controller.catalog.cache.QueryCache` cache pattern, reused
read-only) and :func:`seestar_controller.store.frames.header_to_dict`.

Pipeline (issue 01 of gaia-crossmatch — detection + query + cache; storing
the table into the project is issue 02):

1. :func:`~.detect.detect_sources` — photutils ``DAOStarFinder`` over the
   stack (FWHM estimated from the image, configurable threshold) with the
   WCS read from the stack FITS header; a header without a celestial WCS
   raises :class:`~.errors.WcsMissingError` ("plate-solve the stack first");
2. :func:`~.query.query_gaia_cached` — a Gaia DR3 cone search around the
   field center (radius = field diagonal + margin) via direct VizieR TAP
   HTTP, selecting G/BP/RP/parallax/pmRA/pmDE with ``G < 20``, cached on
   disk under ``<config>/cache/gaia/`` keyed by the rounded center + radius
   with a TTL (the catalog module's cache pattern); a query failure with no
   cache raises :class:`~.errors.GaiaUnavailableError`;
3. :func:`~.matching.crossmatch` — pair every detection with its nearest
   Gaia row within a configurable radius (default 1.5 arcsec); unmatched
   detections are flagged.

:func:`~.matching.crossmatch_stack` runs the whole pipeline over one
stack. The single HTTP boundary is :func:`~.query.fetch_gaia_tap_json` —
tests mock it, so no test ever touches the network.

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
  :func:`~.matching.crossmatch`, :func:`~.matching.crossmatch_stack`.
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

__all__ = [
    "ARCSEC_PER_DEG",
    "CACHE_DIR_ENV_VAR",
    "DEFAULT_CACHE_DIR",
    "DEFAULT_FWHM_PX",
    "DEFAULT_G_LIMIT",
    "DEFAULT_MARGIN_DEG",
    "DEFAULT_MATCH_RADIUS_ARCSEC",
    "DEFAULT_MAXREC",
    "DEFAULT_THRESHOLD_SIGMA",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_TTL_S",
    "GAIA_COLUMNS",
    "GAIA_DR3_TABLE",
    "KEY_ROUND_DIGITS",
    "MAX_FWHM_PX",
    "MAX_RADIUS_DEG",
    "MIN_FWHM_PX",
    "VIZIER_TAP_SYNC_URL",
    "CrossmatchRow",
    "DetectedSource",
    "DetectionError",
    "GaiaError",
    "GaiaResponseError",
    "GaiaStar",
    "GaiaUnavailableError",
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
    "parse_gaia_response",
    "pixel_to_sky",
    "query_gaia",
    "query_gaia_cached",
]
