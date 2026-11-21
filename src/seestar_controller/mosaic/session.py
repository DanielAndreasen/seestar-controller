"""Session stitching — stitch one mosaic session's tile stacks into one FITS.

Pure module of :mod:`seestar_controller.mosaic` (mosaic-stitch ticket 01):
**no ``gi`` import** — stdlib + :mod:`seestar_controller.store` +
:mod:`.core` only; the heavy :mod:`astropy.io.fits` import stays lazy inside
:func:`stitch_session` (the same pattern as
:mod:`seestar_controller.frame_quality.session`).

- :data:`MOSAICS_DIR` / :func:`mosaic_path` — the mosaics live FILE-BASED in
  the project directory (``<project>/mosaics/<session_id>.fits``); the store
  schema is NOT bumped and the mosaic is NOT registered as a ``frames`` row
  (it is a derived product, like an export);
- :func:`stitch_session` — collect the session's tile stacks
  (``store.frames`` ``kind="stack"``), read each tile's WCS, stitch them on
  their common grid (:func:`~.core.stitch_tiles`) and write the stitched FITS
  atomically (temp file in the same directory + fsync + :func:`os.replace`,
  temp removed on any failure) into ``mosaics/<session_id>.fits``, returning
  its path.

Documented choices:

- **Tile stacks only.** The tiles of a mosaic session are its ``kind="stack"``
  frames (subs are the raw exposures, enhanced images are per-tile products);
  a session with no stacks raises :class:`~.core.MosaicError`.
- **Unknown session** raises
  :class:`~seestar_controller.store.errors.SessionNotFoundError` (a lookup,
  not a filter — the same pattern as
  :func:`seestar_controller.frame_quality.session.score_session`).
- **Unreadable tiles fail loudly** (unlike scoring, stitching cannot skip a
  tile): a stack whose bytes cannot be read or parsed, or whose data is
  missing, raises :class:`~.core.MosaicError` naming the frame (path + id);
  a tile without a celestial WCS or a disconnected overlap raises
  :class:`~.core.MosaicError` from :func:`~.core.stitch_tiles`.
- **Overwrite**: ``overwrite=True`` (default) replaces an existing mosaic;
  ``overwrite=False`` raises :class:`~.core.MosaicError` when the target
  already exists.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from seestar_controller.store.errors import ProjectError, SessionNotFoundError
from seestar_controller.store.frames import (
    KIND_STACK,
    Frame,
    header_to_dict,
    list_frames,
    read_frame_bytes,
)
from seestar_controller.store.project import Project

from .core import (
    BLEND_AVERAGE,
    DEFAULT_FEATHER_PX,
    MosaicError,
    Tile,
    stitch_tiles,
)

logger = logging.getLogger(__name__)

#: The directory (inside the project) holding the stitched mosaics.
MOSAICS_DIR = "mosaics"

#: FITS cards managed by the writer itself (or meaningless on output) —
#: dropped from the reference tile's header when writing the mosaic.
_DROP_CARDS = frozenset(
    {
        "SIMPLE",
        "BITPIX",
        "NAXIS",
        "NAXIS1",
        "NAXIS2",
        "NAXIS3",
        "BSCALE",
        "BZERO",
        "EXTEND",
        "CHECKSUM",
        "DATASUM",
        "PCOUNT",
        "GCOUNT",
    }
)


def mosaic_path(project: Project, session_id: str) -> Path:
    """The path of the stitched mosaic of ``session_id``:
    ``<project>/mosaics/<session_id>.fits`` (the file may not exist yet)."""
    if not isinstance(session_id, str):
        raise TypeError(f"session_id must be a string, got {session_id!r}")
    return project.path / MOSAICS_DIR / f"{session_id}.fits"


def _load_tile(project: Project, frame: Frame) -> Tile:
    """Read one stored stack frame and return it as a :class:`Tile`.

    Raises :class:`MosaicError` naming the frame (path + id) when its bytes
    cannot be read or parsed, or when it has no image data.
    """
    from astropy.io import fits as pyfits

    name = f"{frame.path} (frame {frame.id})"
    try:
        fits_bytes = read_frame_bytes(project, frame)
    except ProjectError as exc:
        raise MosaicError(f"cannot read tile {name!r}: {exc}") from exc
    try:
        with pyfits.open(io.BytesIO(fits_bytes), memmap=False) as hdul:
            data = hdul[0].data
            header = header_to_dict(hdul[0].header)
    except Exception as exc:
        raise MosaicError(f"cannot parse tile {name!r} as FITS: {exc}") from exc
    if data is None:
        raise MosaicError(f"tile {name!r} has no image data in its primary HDU")
    return Tile(name=name, data=data, header=header)


def _write_mosaic(dest: Path, mosaic: np.ndarray, cards: dict[str, Any], *, n_tiles: int, blend: str, feather_px: float) -> None:
    """Write the stitched mosaic FITS to ``dest`` atomically (temp file in
    the same directory + fsync + :func:`os.replace`; the temp file is
    removed on any failure, incl. :class:`KeyboardInterrupt`)."""
    from astropy.io import fits as pyfits

    dest.parent.mkdir(parents=True, exist_ok=True)
    header = pyfits.Header(
        {key: value for key, value in cards.items() if key not in _DROP_CARDS}
    )
    header["NMOSAIC"] = (n_tiles, "number of tiles stitched")
    header["BLEND"] = (blend, "overlap blend mode")
    header["FEATHER"] = (float(feather_px), "feather width in pixels")
    header["HISTORY"] = f"stitched from {n_tiles} tile stack(s) by seestar_controller.mosaic"
    hdu = pyfits.PrimaryHDU(data=np.asarray(mosaic, dtype=np.float32), header=header)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".mosaic-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            hdu.writeto(fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.close(fd)  # already closed when the with-block exited
        except OSError:
            pass
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def stitch_session(
    project: Project,
    session_id: str,
    *,
    blend: str = BLEND_AVERAGE,
    feather_px: float = DEFAULT_FEATHER_PX,
    use_reproject: bool | None = None,
    overwrite: bool = True,
) -> Path:
    """Stitch ALL tile stacks of the session ``session_id`` in ``project``
    into one wide-field FITS and return its path
    (``mosaics/<session_id>.fits``, see :func:`mosaic_path`).

    - ``session_id`` — must exist in the project, else
      :class:`~seestar_controller.store.errors.SessionNotFoundError`; a
      session with no tile stacks raises :class:`~.core.MosaicError`;
    - ``blend`` — the overlap blend mode, :data:`~.core.BLEND_AVERAGE`
      (default) or :data:`~.core.BLEND_FEATHER`;
    - ``feather_px`` — the feather width in pixels (default
      :data:`~.core.DEFAULT_FEATHER_PX`);
    - ``use_reproject`` — None (default) = reproject onto the common grid
      when the optional :mod:`reproject` package is installed, else the
      integer-shift fallback by the WCS delta; True = require reproject;
      False = always the fallback (see :func:`~.core.stitch_tiles`);
    - ``overwrite`` — when False, an existing mosaic file raises
      :class:`~.core.MosaicError` instead of being replaced.

    Each tile stack (``kind="stack"``) is read from the project and turned
    into a :class:`~.core.Tile`; the tiles are stitched with
    :func:`~.core.stitch_tiles` (a tile without a celestial WCS, or tiles
    that do not overlap, raise :class:`~.core.MosaicError` naming the
    tile(s)). The mosaic is written atomically into the project and its path
    returned. The mosaic is NOT registered in the ``frames`` table.
    """
    if not isinstance(session_id, str):
        raise TypeError(f"session_id must be a string, got {session_id!r}")
    if not isinstance(overwrite, bool):
        raise TypeError(f"overwrite must be a bool, got {overwrite!r}")
    row = project.db.execute(
        "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise SessionNotFoundError(
            f"project {project.slug!r} has no session {session_id!r}"
        )

    frames = list_frames(project, session_id=session_id, kind=KIND_STACK)
    if not frames:
        raise MosaicError(
            f"session {session_id!r} of project {project.slug!r} has no tile "
            "stacks (kind='stack') to stitch"
        )
    tiles = [_load_tile(project, frame) for frame in frames]

    mosaic, cards = stitch_tiles(
        tiles, blend=blend, feather_px=feather_px, use_reproject=use_reproject
    )

    dest = mosaic_path(project, session_id)
    if dest.exists() and not overwrite:
        raise MosaicError(
            f"mosaic {dest} already exists and overwrite=False — remove it "
            "or pass overwrite=True"
        )
    _write_mosaic(
        dest, mosaic, cards, n_tiles=len(tiles), blend=blend, feather_px=feather_px
    )
    logger.info(
        "stitched %d tile(s) of session %s into %s", len(tiles), session_id, dest
    )
    return dest
