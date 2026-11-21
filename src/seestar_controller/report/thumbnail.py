"""Stack thumbnail — PIL PNG rendering of a session's best stack frame for
session-report ticket 01 (:mod:`seestar_controller.report.thumbnail`).

This is a PURE module: **no ``gi`` import** (a subprocess test enforces the
boundary). :mod:`numpy` and :mod:`PIL` (Pillow) are hard dependencies and
imported at module top; :mod:`astropy` (FITS reading) is imported LAZILY
inside the reader, the same pattern as :mod:`seestar_controller.store.frames`
— this package must not import :mod:`seestar_controller.enhance` (its
``fits_io`` does the same job, but the enhance package is out of this
module's boundary).

What lives here:

- :data:`THUMBNAIL_MAX_PX` — the thumbnail's maximum width/height in pixels;
- :func:`stack_thumbnail_png` — render the FITS file of ``frame`` (a store
  :class:`~seestar_controller.store.frames.Frame`) to a PNG thumbnail:
  read the first HDU with image data (astropy, lazy), normalize to float,
  percentile-stretch (1%–99%) to 8-bit RGB, downscale with PIL's
  :meth:`~PIL.Image.Image.thumbnail` to at most :data:`THUMBNAIL_MAX_PX` on
  the long side, and encode as PNG bytes.

Documented choices:

- **Never raises.** A missing/unreadable file, a FITS without image data, or
  an unsupported shape yields ``None`` — the report HTML then shows a
  placeholder instead of an image (a report must always generate).
- **Stretch.** A robust 1%–99% percentile stretch per channel (global
  percentiles for RGB), clipped to [0, 1]; NaN/inf pixels become black
  (0). A degenerate range (flat image) maps to mid-gray, not a crash.
- **RGB handling.** ``(H, W, 3|4)`` stays RGB (alpha dropped); FITS-order
  ``(3|4, H, W)`` is transposed to ``(H, W, 3)``; anything else 3-D falls
  back to its first 2-D slice.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from seestar_controller.store.frames import Frame, get_frame_path
from seestar_controller.store.project import Project

#: Maximum width/height of the thumbnail in pixels (the long side).
THUMBNAIL_MAX_PX = 512

#: Percentile stretch bounds (low, high) in percent.
STRETCH_PERCENTILES = (1.0, 99.0)


def _read_fits_array(path: Path) -> np.ndarray:
    """Read the first HDU with image data of the FITS file at ``path`` into a
    float64 array (2-D, or RGB ``(H, W, 3)``), NaN-safe (every non-finite
    value becomes NaN). Lazy astropy import.

    Raises :class:`ValueError` when the file is unreadable / has no image
    data / has an unsupported shape.
    """
    from astropy.io import fits as pyfits

    with pyfits.open(path, memmap=False) as hdul:
        hdu = next((h for h in hdul if getattr(h, "data", None) is not None), None)
        if hdu is None:
            raise ValueError(f"{path} has no image data in any HDU")
        raw = np.asarray(hdu.data)
    arr = np.asarray(raw, dtype=np.float64)
    if arr.ndim == 2:
        pass
    elif arr.ndim == 3 and arr.shape[-1] in (3, 4):
        arr = arr[..., :3]  # (H, W, 3|4) -> RGB
    elif arr.ndim == 3 and arr.shape[0] in (3, 4):
        arr = np.transpose(arr[:3], (1, 2, 0))  # FITS (3|4, H, W) -> (H, W, 3)
    elif arr.ndim == 3:
        arr = arr[0]  # unsupported channel count: first 2-D slice
    else:
        raise ValueError(f"unsupported FITS data shape {arr.shape} at {path}")
    arr[~np.isfinite(arr)] = np.nan
    return arr


def _stretch_to_uint8(arr: np.ndarray) -> np.ndarray:
    """Percentile-stretch ``arr`` to a uint8 array in [0, 255].

    2-D input -> 2-D uint8 (grayscale); RGB input -> per-channel stretch with
    GLOBAL percentiles (computed over all channels) -> ``(H, W, 3)`` uint8.
    NaN pixels map to 0 (black). A degenerate range (hi <= lo) maps to 128
    (mid-gray).
    """
    lo_pct, hi_pct = STRETCH_PERCENTILES
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return np.full(arr.shape, 128, dtype=np.uint8)
    lo = float(np.percentile(finite, lo_pct))
    hi = float(np.percentile(finite, hi_pct))
    if not hi > lo:
        return np.full(arr.shape, 128, dtype=np.uint8)
    scaled = np.clip((arr - lo) / (hi - lo), 0.0, 1.0)
    scaled = np.nan_to_num(scaled, nan=0.0)
    return (scaled * 255.0).astype(np.uint8)


def _to_rgb_image(arr: np.ndarray) -> Image.Image:
    """The stretched uint8 array as a PIL RGB image."""
    stretched = _stretch_to_uint8(arr)
    if stretched.ndim == 2:
        return Image.fromarray(stretched, mode="L").convert("RGB")
    return Image.fromarray(stretched, mode="RGB")


def stack_thumbnail_png(
    project: Project,
    frame: Frame | None,
    *,
    max_px: int = THUMBNAIL_MAX_PX,
) -> bytes | None:
    """Render the FITS file of ``frame`` in ``project`` to a PNG thumbnail
    (at most ``max_px`` on the long side) and return the PNG bytes.

    ``frame=None`` yields None. Any read/render failure (missing file,
    unreadable FITS, unsupported shape) also yields None — the thumbnail is
    best-effort and never fails the report.
    """
    if frame is None:
        return None
    if isinstance(max_px, bool) or not isinstance(max_px, int) or max_px <= 0:
        raise TypeError(f"max_px must be a positive int, got {max_px!r}")
    try:
        path = get_frame_path(project, frame)
        arr = _read_fits_array(path)
        image = _to_rgb_image(arr)
        image.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001 — best-effort by contract
        return None


def thumbnail_png_from_array(arr: Any, *, max_px: int = THUMBNAIL_MAX_PX) -> bytes:
    """Render a raw numpy array (2-D or RGB) to PNG thumbnail bytes — the
    array-level core of :func:`stack_thumbnail_png`, unit-testable without a
    FITS file."""
    image = _to_rgb_image(np.asarray(arr, dtype=np.float64))
    image.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()
