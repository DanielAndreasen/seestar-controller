"""hr-diagram — CMD / H-R diagrams of a stack's Gaia cross-match table.

Spec: ``.scratch/hr-diagram/spec.md``. This package is the PURE layer of the
hr-diagram spec: given the gaia-crossmatch table of ONE stack (the persisted
:class:`~seestar_controller.store.crossmatch.CrossmatchRecord` rows from
:func:`~seestar_controller.store.crossmatch.load_crossmatch`, consumed here
duck-typed — this package imports nothing from :mod:`seestar_controller.store`),
it computes (a) the apparent CMD points (G vs BP-RP), (b) the H-R points
(absolute G = ``G + 5 - 5*log10(1000/parallax_mas)`` vs BP-RP, with the
parallax SNR cut ``parallax/parallax_error > 5``), and (c) the cluster
membership by sigma-clipping parallax + proper motion around the median, plus
the CSV export of the table.

**No ``gi`` import anywhere in this package** — stdlib only — fully
unit-testable headless (the sandbox has no GTK, see docs/wiki/assumptions.md;
a subprocess test enforces the boundary). No network either.

Modules (ticket 01 — the computation core; the view is ticket 03):

- :mod:`.core` — :class:`~.core.HrError`, the frozen point record
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
  (:func:`~.isochrone.fallback_isochrone`).

Ticket 01 is pure-only: there is no widget module yet, so there is no lazy
PEP 562 export — ``import seestar_controller.hr_diagram`` never imports
``gi``.
"""

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

__all__ = [
    "CSV_HEADER",
    "DEFAULT_PARALLAX_SNR",
    "DEFAULT_SIGMA_CLIP",
    "MAD_TO_SIGMA",
    "MIN_MEMBERSHIP_SAMPLE",
    "PARSEC_ISOCHRONE_URL",
    "ExportError",
    "HrError",
    "HrPoint",
    "IsochroneError",
    "IsochroneFetchError",
    "IsochronePoint",
    "Membership",
    "absolute_g",
    "cmd_points",
    "export_table_csv",
    "fallback_isochrone",
    "fetch_isochrone_json",
    "hr_points",
    "isochrone",
    "membership_filter",
]
