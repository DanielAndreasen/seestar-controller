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
  cross-match rows) and :func:`~.session.solve_session_frame`.

Typical flow::

    from seestar_controller.astrometry import solve_frame, solve_session_frame

    result = solve_frame("stack.fits")                    # or data=..., header=...
    result.cards                                          # WCS as FITS cards

    result = solve_session_frame(project, session_id, frame_id)
    result.center_ra_deg, result.rms_arcsec
"""

from .errors import AstrometryError, SolveError
from .session import solve_project_frame, solve_session_frame
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

__all__ = [
    "ARCSEC_PER_DEG",
    "DEFAULT_MATCH_RADIUS_ARCSEC",
    "DEFAULT_MIN_MATCHES",
    "DEFAULT_PIXEL_SCALE_ARCSEC",
    "DEFAULT_RMS_TOLERANCE_ARCSEC",
    "DEFAULT_SOLVE_FIELD_TIMEOUT_S",
    "FIRST_PASS_RADIUS_FACTOR",
    "SOLVERS",
    "SOLVER_AUTO",
    "SOLVER_GAIA",
    "SOLVER_SOLVE_FIELD",
    "SOLVE_FIELD_BINARY",
    "AstrometryError",
    "GaiaQuery",
    "SolveError",
    "SolveResult",
    "StarMatch",
    "cards_to_wcs",
    "has_celestial_wcs",
    "header_center",
    "header_pixel_scale_arcsec",
    "provisional_wcs_cards",
    "read_fits_bytes",
    "solve_fits_file",
    "solve_frame",
    "solve_project_frame",
    "solve_session_frame",
    "wcs_to_cards",
    "write_wcs_to_fits",
]
