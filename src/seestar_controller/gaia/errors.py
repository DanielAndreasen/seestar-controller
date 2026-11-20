"""Error taxonomy of the gaia-crossmatch pure core.

Pure-Python (module boundary per ``.scratch/gaia-crossmatch/spec.md``): no
GTK imports, no :mod:`seestar_controller.link` imports — a subprocess test
enforces the boundary. Every failure of this package raises one of these
types with a clear, actionable message — never a raw traceback.
"""

from __future__ import annotations


class GaiaError(Exception):
    """Base class for every gaia-crossmatch failure."""


class DetectionError(GaiaError, ValueError):
    """Source detection failed — a bad image, a bad argument, a WCS that
    cannot be built, or a WCS that yields non-finite sky positions."""


class WcsMissingError(DetectionError):
    """The stack FITS header carries no celestial WCS — the stack has not
    been plate-solved yet. The message says so explicitly ("plate-solve
    first"), because detection -> sky positions -> Gaia cross-match is
    impossible without a WCS."""


class GaiaUnavailableError(GaiaError):
    """The Gaia DR3 query could not be performed (network/HTTP failure) and
    the disk cache holds no usable entry for the field — the caller should
    retry when back online."""


class GaiaResponseError(GaiaError):
    """The Gaia TAP endpoint answered, but the response is malformed or
    unusable (not JSON, missing columns, non-numeric values)."""
