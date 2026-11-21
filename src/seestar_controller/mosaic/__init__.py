"""Mosaic stitching — align a mosaic session's tile stacks by their WCS and
stitch them into one wide-field FITS (``.scratch/mosaic-stitch/spec.md``).

Strict pure layer (the sandbox has no GTK, see docs/wiki/assumptions.md):
NOTHING in this package imports ``gi`` — a subprocess test enforces the
boundary. The package is stdlib + :mod:`numpy` +
:mod:`seestar_controller.store` + :mod:`.core` / :mod:`.session`; the heavy
third-party imports (:mod:`astropy.io.fits`, :mod:`astropy.wcs`, and the
optional :mod:`reproject`) are lazy inside the functions that need them, the
same pattern as :mod:`seestar_controller.frame_quality`.

- :mod:`.core` — the pure stitch math: :class:`~.core.Tile` (name + image
  array + FITS header), :func:`~.core.tile_wcs` (the tile's celestial WCS,
  :class:`~.core.MosaicError` naming the tile when missing),
  :func:`~.core.tile_offset_px` (the WCS delta), :func:`~.core.common_grid`
  (the common canvas + the overlap check) and :func:`~.core.stitch_tiles`
  (place every tile on the grid — reproject when the optional
  :mod:`reproject` is installed, else an integer shift by the WCS delta —
  and blend the overlaps, average or feather). Errors are
  :class:`~.core.MosaicError` (a :class:`ValueError`).
- :mod:`.session` — the project integration: :func:`~.session.stitch_session`
  collects the tile stacks (``kind="stack"``) of one session, stitches them,
  and writes the stitched FITS file-based into the project
  (``mosaics/<session_id>.fits`` — NO store schema bump; the mosaic is not a
  ``frames`` row), returning its path. :func:`~.session.mosaic_path` is the
  path helper.

Typical flow::

    from seestar_controller.mosaic import stitch_session, mosaic_path

    path = stitch_session(project, session_id)   # mosaics/<session_id>.fits
"""

from .core import (
    BLEND_AVERAGE,
    BLEND_FEATHER,
    BLENDS,
    DEFAULT_FEATHER_PX,
    SCALE_TOLERANCE,
    Grid,
    MosaicError,
    Tile,
    common_grid,
    stitch_tiles,
    tile_offset_px,
    tile_wcs,
)
from .session import (
    MOSAICS_DIR,
    mosaic_path,
    stitch_session,
)

__all__ = [
    "BLENDS",
    "BLEND_AVERAGE",
    "BLEND_FEATHER",
    "DEFAULT_FEATHER_PX",
    "MOSAICS_DIR",
    "SCALE_TOLERANCE",
    "Grid",
    "MosaicError",
    "Tile",
    "common_grid",
    "mosaic_path",
    "stitch_session",
    "stitch_tiles",
    "tile_offset_px",
    "tile_wcs",
]
