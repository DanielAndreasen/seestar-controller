"""Error taxonomy of the astrometry package.

Pure-Python (module boundary per ``.scratch/astrometry/spec.md``): no GTK
imports, no :mod:`seestar_controller.link` imports, stdlib only.
"""


class AstrometryError(Exception):
    """Base class for all astrometry failures."""


class SolveError(AstrometryError, ValueError):
    """A frame could not be plate-solved: no solver available, the solver
    failed (``solve-field`` nonzero exit/timeout, Gaia query failure), too
    few stars detected or matched, the match RMS exceeded the tolerance, or
    the frame has no center hint for the blind Gaia fallback.

    Subclasses both :class:`AstrometryError` and :class:`ValueError` — a
    failed solve is a value problem, so callers may catch either. The
    message always says WHY the frame could not be solved.
    """
