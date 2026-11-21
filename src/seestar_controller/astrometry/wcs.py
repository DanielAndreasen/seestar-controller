"""WCS card helpers — pure stdlib, astropy lazy inside the functions.

Pure-Python (module boundary per ``.scratch/astrometry/spec.md``): no GTK
imports, no :mod:`seestar_controller.link` imports. This module never imports
astropy at module level — :func:`cards_to_wcs` and :func:`write_wcs_to_fits`
import it inside the function, so ``import seestar_controller.astrometry``
stays astropy-free (a subprocess boundary test enforces it).

A solved WCS is carried around as a plain ``dict`` of FITS cards (the same
shape :func:`seestar_controller.store.frames.header_to_dict` produces), so
the pure core and the project store can hold it without astropy; astropy
enters only when a real :class:`astropy.wcs.WCS` object or a FITS rewrite is
needed.

- :func:`has_celestial_wcs` / :func:`header_center` /
  :func:`header_pixel_scale_arcsec` — pure card inspection of a header;
- :func:`provisional_wcs_cards` — a rough TAN WCS around a center hint (used
  to give the Gaia fallback's detection pass a sky grid);
- :func:`wcs_to_cards` / :func:`cards_to_wcs` — astropy WCS <-> cards dict;
- :func:`write_wcs_to_fits` — atomically write WCS cards into a FITS file's
  primary header (temp file in the same directory + fsync + ``os.replace``).
"""

from __future__ import annotations

import math
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from seestar_controller.store.frames import header_to_dict

from .errors import SolveError

#: FITS keyword prefixes that belong to a WCS — removed from a header before
#: new WCS cards are written (so a stale ``CDELT`` never survives next to a
#: new ``CD`` matrix, which astropy would silently prefer).
_WCS_KEY_RE = re.compile(
    r"^(CTYPE|CRVAL|CRPIX|CDELT|CUNIT|LONPOLE|LATPOLE|RADESYS|WCSAXES|"
    r"MJDREF|MJD-OBS|EQUINOX|CD\d_\d|PC\d_\d|PV\d_\d|PS\d_\d)"
)

#: Arcseconds per degree.
ARCSEC_PER_DEG = 3600.0


def _as_header(header: Any) -> dict[str, Any]:
    """Validate ``header`` is a mapping of FITS cards and return it as a
    sanitized plain dict (see
    :func:`seestar_controller.store.frames.header_to_dict`)."""
    if not isinstance(header, Mapping):
        raise TypeError(f"header must be a mapping of FITS cards, got {header!r}")
    return header_to_dict(header)


def has_celestial_wcs(header: Any) -> bool:
    """True when ``header`` carries a celestial WCS (RA/DEC ``CTYPE`` cards
    plus ``CRVAL``) — pure card inspection, no astropy."""
    cards = _as_header(header)
    ctype1 = str(cards.get("CTYPE1", "")).upper()
    ctype2 = str(cards.get("CTYPE2", "")).upper()
    return (
        "RA" in ctype1
        and "DEC" in ctype2
        and cards.get("CRVAL1") is not None
        and cards.get("CRVAL2") is not None
    )


def header_center(header: Any) -> tuple[float, float] | None:
    """The approximate field center ``(ra_deg, dec_deg)`` of ``header``, or
    None when the header carries no center at all.

    Prefers the WCS reference point (``CRVAL1``/``CRVAL2`` of a celestial
    WCS); falls back to plain ``RA``/``DEC`` cards (some scope FITS carry
    those instead of a full WCS). RA is normalized to ``[0, 360)``.
    """
    cards = _as_header(header)
    if has_celestial_wcs(cards):
        ra = float(cards["CRVAL1"]) % 360.0
        dec = float(cards["CRVAL2"])
    elif cards.get("RA") is not None and cards.get("DEC") is not None:
        ra = float(cards["RA"]) % 360.0
        dec = float(cards["DEC"])
    else:
        return None
    if not (math.isfinite(ra) and math.isfinite(dec)) or not -90.0 <= dec <= 90.0:
        return None
    return ra, dec


def header_pixel_scale_arcsec(header: Any) -> float | None:
    """The approximate pixel scale of ``header`` in arcsec/px, or None when
    the header carries no scale.

    Uses ``CDELT2`` (absolute value) when present, else the ``CD`` matrix
    (``sqrt(|det CD|)`` — the determinant is negative for the usual RA-flip
    ``CDELT1 < 0`` convention), else the ``PC`` matrix times ``CDELT``, else
    ``CDELT1``. Pure card math, no astropy.
    """
    cards = _as_header(header)

    def _num(key: str) -> float | None:
        value = cards.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        result = float(value)
        return result if math.isfinite(result) else None

    def _scale(deg_per_px: float) -> float | None:
        scale = abs(deg_per_px) * ARCSEC_PER_DEG
        return scale if math.isfinite(scale) and scale > 0 else None

    cdelt2 = _num("CDELT2")
    if cdelt2 is not None:
        found = _scale(cdelt2)
        if found is not None:
            return found
    cd_keys = ("CD1_1", "CD1_2", "CD2_1", "CD2_2")
    if all(_num(k) is not None for k in cd_keys):
        cd11, cd12, cd21, cd22 = (float(cards[k]) for k in cd_keys)
        det = abs(cd11 * cd22 - cd12 * cd21)
        if det > 0:
            found = _scale(math.sqrt(det))
            if found is not None:
                return found
    pc_keys = ("PC1_1", "PC1_2", "PC2_1", "PC2_2")
    cdelt1 = _num("CDELT1")
    if cdelt1 is not None and all(_num(k) is not None for k in pc_keys):
        pc11, pc12, pc21, pc22 = (float(cards[k]) for k in pc_keys)
        det = abs(pc11 * pc22 - pc12 * pc21)
        if det > 0:
            found = _scale(math.sqrt(det) * abs(cdelt1))
            if found is not None:
                return found
    if cdelt1 is not None:
        return _scale(cdelt1)
    return None


def provisional_wcs_cards(
    center_ra_deg: float,
    center_dec_deg: float,
    scale_arcsec_per_px: float,
    shape: tuple[int, int],
) -> dict[str, Any]:
    """A rough TAN WCS around a center hint, as a dict of FITS cards.

    - ``center_ra_deg`` / ``center_dec_deg`` — the approximate field center
      (J2000 degrees; RA normalized to ``[0, 360)``, Dec clamped-checked to
      ``[-90, 90]``);
    - ``scale_arcsec_per_px`` — the approximate pixel scale in arcsec/px
      (must be a finite number > 0);
    - ``shape`` — the image shape ``(ny, nx)``; the reference pixel is the
      1-based image center.

    The result is a NORTH-UP, unrotated TAN projection with ``CRVAL`` at the
    hint and ``CDELT1`` negative (RA increases to the left, the usual FITS
    convention) — good enough to give the Gaia fallback's detection pass a
    sky grid and a cone-search radius.
    """
    if isinstance(center_ra_deg, bool) or not isinstance(center_ra_deg, (int, float)):
        raise TypeError(f"center_ra_deg must be a number, got {center_ra_deg!r}")
    if isinstance(center_dec_deg, bool) or not isinstance(center_dec_deg, (int, float)):
        raise TypeError(f"center_dec_deg must be a number, got {center_dec_deg!r}")
    ra = float(center_ra_deg)
    dec = float(center_dec_deg)
    if not math.isfinite(ra) or not math.isfinite(dec) or not -90.0 <= dec <= 90.0:
        raise ValueError(
            f"center must be a finite (ra, dec) with dec in [-90, 90], "
            f"got ({center_ra_deg!r}, {center_dec_deg!r})"
        )
    if (
        isinstance(scale_arcsec_per_px, bool)
        or not isinstance(scale_arcsec_per_px, (int, float))
        or not math.isfinite(scale_arcsec_per_px)
        or scale_arcsec_per_px <= 0
    ):
        raise ValueError(
            f"scale_arcsec_per_px must be a finite number > 0, got {scale_arcsec_per_px!r}"
        )
    if (
        not isinstance(shape, (tuple, list))
        or len(shape) != 2
        or isinstance(shape[0], bool)
        or not isinstance(shape[0], (int,))
        or isinstance(shape[1], bool)
        or not isinstance(shape[1], (int,))
        or shape[0] < 1
        or shape[1] < 1
    ):
        raise ValueError(f"shape must be a (ny, nx) pair of positive ints, got {shape!r}")
    ny, nx = int(shape[0]), int(shape[1])
    scale_deg = float(scale_arcsec_per_px) / ARCSEC_PER_DEG
    return {
        "WCSAXES": 2,
        "CTYPE1": "RA---TAN",
        "CTYPE2": "DEC--TAN",
        "CUNIT1": "deg",
        "CUNIT2": "deg",
        "CRVAL1": ra % 360.0,
        "CRVAL2": dec,
        "CRPIX1": (nx + 1) / 2.0,
        "CRPIX2": (ny + 1) / 2.0,
        "CDELT1": -scale_deg,
        "CDELT2": scale_deg,
    }


def wcs_to_cards(wcs: Any) -> dict[str, Any]:
    """An astropy :class:`astropy.wcs.WCS` -> a plain dict of FITS cards
    (sanitized, see :func:`seestar_controller.store.frames.header_to_dict`).

    The cards are exactly what :func:`cards_to_wcs` (and a FITS header)
    rebuild the WCS from.
    """
    return header_to_dict(wcs.to_header())


def cards_to_wcs(cards: Mapping[str, Any]) -> Any:
    """A dict of FITS cards -> an astropy :class:`astropy.wcs.WCS` (lazy
    astropy import — this module stays astropy-free at import time).

    Raises :class:`SolveError` when the cards do not build a 2-D WCS.
    """
    from astropy.io import fits as pyfits
    from astropy.wcs import WCS

    if not isinstance(cards, Mapping):
        raise TypeError(f"cards must be a mapping of FITS cards, got {cards!r}")
    plain = {str(k): v for k, v in cards.items()}
    try:
        wcs = WCS(pyfits.Header(plain), naxis=2)
    except Exception as exc:
        raise SolveError(f"cannot build a WCS from the cards: {exc}") from exc
    if wcs.naxis < 2:
        raise SolveError("the cards do not describe a 2-D WCS")
    return wcs


def _check_cards(cards: Any) -> dict[str, Any]:
    if not isinstance(cards, Mapping):
        raise TypeError(f"cards must be a mapping of FITS cards, got {cards!r}")
    plain = {str(key): value for key, value in cards.items()}
    for key in plain:
        if len(key) > 8:
            raise ValueError(f"FITS keyword {key!r} is longer than 8 characters")
    return plain


def write_wcs_to_fits(path: str | Path, cards: Mapping[str, Any]) -> Path:
    """Write the WCS ``cards`` into the primary header of the FITS file
    ``path`` — atomically (the whole file is rewritten to a temp file in the
    SAME directory, fsynced, then ``os.replace``d over ``path``; the temp
    file is removed on any failure incl. :class:`KeyboardInterrupt`), so a
    crash mid-write never leaves a half-written FITS.

    All pre-existing WCS keywords (``CTYPE``/``CRVAL``/``CRPIX``/``CDELT``/
    ``CD*``/``PC*``/...) are removed first, so a stale scale never survives
    next to the new cards. Returns the path written.

    Raises :class:`TypeError` for a non-mapping ``cards``, :class:`ValueError`
    for an over-long keyword, and :class:`SolveError` when ``path`` is not a
    readable FITS file or the rewrite fails.
    """
    from astropy.io import fits as pyfits

    plain = _check_cards(cards)
    target = Path(path)
    try:
        with pyfits.open(target, memmap=False) as hdul:
            header = hdul[0].header
            for key in list(header.keys()):
                if key and _WCS_KEY_RE.match(key):
                    del header[key]
            header.update(pyfits.Header(plain))
            fd, tmp_name = tempfile.mkstemp(
                dir=target.parent, prefix=".wcs-", suffix=".tmp"
            )
            os.close(fd)
            tmp = Path(tmp_name)
            try:
                hdul.writeto(tmp, overwrite=True)
                with open(tmp, "rb+") as fh:
                    os.fsync(fh.fileno())
                os.replace(tmp, target)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
    except SolveError:
        raise
    except Exception as exc:
        raise SolveError(f"cannot write the WCS into {target}: {exc}") from exc
    return target
