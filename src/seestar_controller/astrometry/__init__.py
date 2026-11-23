"""Astrometry — plate-solve frames on demand and persist the solved WCS
(``.scratch/astrometry/spec.md``, issue 01: the solve core).

Strict pure layer (the sandbox has no GTK, see docs/wiki/assumptions.md):
NOTHING in this package imports ``gi`` — a subprocess test enforces the
boundary. The package is stdlib + :mod:`numpy` + the pure
:mod:`seestar_controller.catalog` / :mod:`seestar_controller.gaia` /
:mod:`seestar_controller.store` packages; astropy and photutils are lazy
inside the functions that need them, and the local astrometry.net
``solve-field`` binary runs as a subprocess (the package's only subprocess).

- :mod:`.errors` — the error taxonomy: :class:`~.errors.AstrometryError`
  base and :class:`~.errors.SolveError` (a :class:`ValueError` with a clear
  "why could this frame not be solved" message);
- :mod:`.wcs` — WCS card helpers, pure stdlib: :func:`~.wcs.has_celestial_wcs`,
  :func:`~.wcs.header_center`, :func:`~.wcs.header_pixel_scale_arcsec`,
  :func:`~.wcs.provisional_wcs_cards` (a rough TAN WCS around a center hint),
  :func:`~.wcs.wcs_to_cards` / :func:`~.wcs.cards_to_wcs` (astropy WCS <->
  cards dict, lazy), and :func:`~.wcs.write_wcs_to_fits` (atomic WCS write
  into a FITS header);
- :mod:`.solve` — the solver core: :func:`~.solve.solve_frame` (a FITS path,
  FITS bytes, or data+header -> a :class:`~.solve.SolveResult`) tries the
  local ``solve-field`` binary (``shutil.which`` + subprocess) when
  ``solver="auto"``/``"solve-field"`` and otherwise falls back to the
  pure-Python Gaia pattern match built on the gaia-crossmatch API
  (:func:`~seestar_controller.gaia.detect.detect_sources` +
  :func:`~seestar_controller.gaia.matching.crossmatch` against a Gaia cone
  query, WCS fit with :func:`astropy.wcs.utils.fit_wcs_from_points`);
  :func:`~.solve.solve_fits_file` is the path convenience wrapper;
- :mod:`.session` — the project glue: :func:`~.session.solve_project_frame`
  (solve a stored frame, write the WCS into its FITS header, persist the WCS
  record in the project db — ``frame_wcs`` table, schema v7 — and the
  cross-match rows) and :func:`~.session.solve_session_frame`; issue 10 adds
  the batch solve :func:`~.session.solve_session_unsolved` (every sub of a
  session without a ``frame_wcs`` row, per-frame :class:`~.errors.SolveError`
  tolerated as skipped, frozen :class:`~.session.SessionSolveReport` +
  ``on_progress(done, total, frame)``) and :func:`~.session.session_wcs_coverage`
  (the session's solved/total subs, :class:`~.session.SessionWcsCoverage`);
  issue 16 adds the solve-QUALITY surface:
  :func:`~.session.is_low_confidence_solve` (with
  :data:`~.session.LOW_CONFIDENCE_MIN_STARS` /
  :data:`~.session.LOW_CONFIDENCE_MAX_RMS_ARCSEC`) plus
  ``SessionSolveReport.low_confidence_ids`` and
  ``SessionWcsCoverage.low_confidence`` / ``low_confidence_ids``;
- :mod:`.drift` — the pointing/drift statistics (issue 02, a read-only
  consumer of the ``frame_wcs`` store): :func:`~.drift.pointing_error`
  (requested queue-entry target vs. the solved WCS centers, arcsec) and
  :func:`~.drift.drift_series` (RA/Dec offset vs. frame time across the
  session's solved frames, plus drift rates in arcsec/hour);
- :mod:`.annotate` / :mod:`.export` — the annotated PNG export (issue 03):
  :func:`~.annotate.render_annotated_stack` /
  :func:`~.annotate.annotated_png_bytes` render a stack tone-mapped with the
  cross-match rows (matched stars ringed + labelled with their Gaia G
  magnitude, unmatched flagged), the FOV rectangle with corner sky
  coordinates, and a scale bar (PIL, the same path as the gaia overlay);
  :func:`~.export.annotated_stack_png` / :func:`~.export.export_annotated_png`
  do the same for a stored frame (atomic write);
- :mod:`.plot` — the drift plot: :func:`~.plot.render_drift_png` renders a
  :class:`~.drift.DriftSeries` to PNG bytes (matplotlib Agg, lazy);
- :mod:`.controller` — the app-level controllers (issue 03):
  :class:`~.controller.SolveController` (the image viewer's Solve action +
  annotated export, following the shared viewer controller) and
  :class:`~.controller.DriftController` (the session drift plot's data).

The widget layer (:mod:`.view` — :class:`~.view.SolvePanel` /
:class:`~.view.DriftPlotView`) imports ``gi`` at module top and is re-exported
lazily (PEP 562 ``__getattr__``), never eagerly.

Typical flow::

    from seestar_controller.astrometry import solve_frame, solve_session_frame

    result = solve_frame("stack.fits")                    # or data=..., header=...
    result.cards                                          # WCS as FITS cards

    result = solve_session_frame(project, session_id, frame_id)
    result.center_ra_deg, result.rms_arcsec
"""

from .annotate import (
    FOV_COLOR,
    NICE_SCALE_BAR_ARCSEC,
    SCALE_BAR_COLOR,
    annotated_png_bytes,
    format_scale_bar,
    fov_corner_sky,
    render_annotated_stack,
    scale_bar_arcsec,
)
from .controller import (
    DB_CALL_TIMEOUT_S,
    DriftController,
    DriftSnapshot,
    SolveController,
    SolveSnapshot,
)
from .drift import (
    DriftError,
    DriftPoint,
    DriftSeries,
    PointingError,
    drift_series,
    pointing_error,
    separation_arcsec,
)
from .errors import AstrometryError, ExportError, SolveError
from .export import (
    DEFAULT_EXPORT_MAX_SIZE,
    annotated_stack_png,
    default_export_name,
    export_annotated_png,
)
from .plot import (
    DEFAULT_DPI,
    DEFAULT_HEIGHT_PX,
    DEFAULT_WIDTH_PX,
    MAX_RENDER_PX,
    render_drift_png,
)
from .session import (
    LOW_CONFIDENCE_MAX_RMS_ARCSEC,
    LOW_CONFIDENCE_MIN_STARS,
    SessionSolveReport,
    SessionWcsCoverage,
    SkippedSolve,
    SolvedFrame,
    SolveProgressCallback,
    StoreCall,
    is_low_confidence_solve,
    session_wcs_coverage,
    solve_project_frame,
    solve_session_frame,
    solve_session_unsolved,
)
from .solve import (
    ARCSEC_PER_DEG,
    DEFAULT_MATCH_RADIUS_ARCSEC,
    DEFAULT_MIN_MATCHES,
    DEFAULT_PIXEL_SCALE_ARCSEC,
    DEFAULT_RMS_TOLERANCE_ARCSEC,
    DEFAULT_SOLVE_FIELD_TIMEOUT_S,
    FIRST_PASS_RADIUS_FACTOR,
    SOLVE_FIELD_BINARY,
    SOLVER_AUTO,
    SOLVER_GAIA,
    SOLVER_SOLVE_FIELD,
    SOLVERS,
    GaiaQuery,
    SolveResult,
    StarMatch,
    read_fits_bytes,
    solve_fits_file,
    solve_frame,
)
from .wcs import (
    cards_to_wcs,
    has_celestial_wcs,
    header_center,
    header_pixel_scale_arcsec,
    provisional_wcs_cards,
    wcs_to_cards,
    write_wcs_to_fits,
)

_WIDGET_EXPORTS = {
    "DriftPlotView": "view",
    "SolvePanel": "view",
}


def __getattr__(name: str):
    if name in _WIDGET_EXPORTS:
        from importlib import import_module

        module = import_module(f".{_WIDGET_EXPORTS[name]}", __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ARCSEC_PER_DEG",
    "DB_CALL_TIMEOUT_S",
    "DEFAULT_DPI",
    "DEFAULT_EXPORT_MAX_SIZE",
    "DEFAULT_HEIGHT_PX",
    "DEFAULT_MATCH_RADIUS_ARCSEC",
    "DEFAULT_MIN_MATCHES",
    "DEFAULT_PIXEL_SCALE_ARCSEC",
    "DEFAULT_RMS_TOLERANCE_ARCSEC",
    "DEFAULT_SOLVE_FIELD_TIMEOUT_S",
    "DEFAULT_WIDTH_PX",
    "FIRST_PASS_RADIUS_FACTOR",
    "FOV_COLOR",
    "LOW_CONFIDENCE_MAX_RMS_ARCSEC",
    "LOW_CONFIDENCE_MIN_STARS",
    "MAX_RENDER_PX",
    "NICE_SCALE_BAR_ARCSEC",
    "SCALE_BAR_COLOR",
    "SOLVERS",
    "SOLVER_AUTO",
    "SOLVER_GAIA",
    "SOLVER_SOLVE_FIELD",
    "SOLVE_FIELD_BINARY",
    "AstrometryError",
    "DriftController",
    "DriftError",
    "DriftPlotView",
    "DriftPoint",
    "DriftSeries",
    "DriftSnapshot",
    "ExportError",
    "GaiaQuery",
    "PointingError",
    "SessionSolveReport",
    "SessionWcsCoverage",
    "SkippedSolve",
    "SolveController",
    "SolveError",
    "SolvePanel",
    "SolveProgressCallback",
    "SolveResult",
    "SolveSnapshot",
    "SolvedFrame",
    "StarMatch",
    "StoreCall",
    "annotated_png_bytes",
    "annotated_stack_png",
    "cards_to_wcs",
    "default_export_name",
    "drift_series",
    "export_annotated_png",
    "format_scale_bar",
    "fov_corner_sky",
    "has_celestial_wcs",
    "header_center",
    "header_pixel_scale_arcsec",
    "is_low_confidence_solve",
    "pointing_error",
    "provisional_wcs_cards",
    "read_fits_bytes",
    "render_annotated_stack",
    "render_drift_png",
    "scale_bar_arcsec",
    "separation_arcsec",
    "session_wcs_coverage",
    "solve_fits_file",
    "solve_frame",
    "solve_project_frame",
    "solve_session_frame",
    "solve_session_unsolved",
    "wcs_to_cards",
    "write_wcs_to_fits",
]
