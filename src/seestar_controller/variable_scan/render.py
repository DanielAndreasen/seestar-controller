"""Cutout rendering + the blink GIF — the PURE render layer of variable-scan
ticket 02 (:mod:`seestar_controller.variable_scan.render`).

Pure Python: **no ``gi`` import** (a subprocess test enforces the boundary) —
stdlib + numpy + PIL + :mod:`seestar_controller.store` only; the heavy
:mod:`astropy.io.fits` import is lazy inside :func:`_read_frame_data`, the
same pattern as :mod:`seestar_controller.variable_scan.scan`. The widget
layer (:mod:`.view`) imports ``gi`` at module top and calls into this module.

What lives here:

- :func:`tonemap_gray` — a LOCAL percentile tone-map of a 2-D image to uint8
  gray (deliberately re-declared, NOT imported from
  :mod:`seestar_controller.moving_objects.overlay` — the package boundary
  forbids cross-spec imports; NaN/inf -> 0);
- :func:`extract_cutout` — the square pixel window around a star position
  (clipped at the image edges, zero-padded);
- :func:`render_cutout_image` — one frame's cutout as an RGB
  :class:`PIL.Image.Image` (tone-mapped gray + a red crosshair at the star);
- :func:`render_blink_frames` — the cutout of ONE star across a sequence of
  sub-frames (the blink frames; unreadable frames are skipped, never a
  crash);
- :func:`render_blink_gif` — the blink frames as ONE animated GIF (PIL
  ``save_all``), returned as bytes — the exportable animation of the star's
  cutout across the frames.

Documented choices:

- **Cutout size** is the full width/height in pixels
  (:data:`DEFAULT_CUTOUT_SIZE`), centered on the star's (rounded) pixel
  position; the window is clipped at the image edges and zero-padded, so a
  star near the border still gets a full-size cutout.
- **GIF**: every frame is converted to palette mode (``P``, adaptive
  palette) and saved with ``save_all=True, loop=0`` and the per-frame
  ``duration`` — a self-contained animated GIF readable by any image viewer
  (and by PIL: ``Image.open`` reports ``n_frames``).
- **Errors**: argument validation raises :class:`~.errors.ScanError`
  immediately; an unreadable frame inside :func:`render_blink_frames` is
  skipped (logged) — the blink of the readable frames is still useful.
"""

from __future__ import annotations

import io
import logging
import math
from collections.abc import Sequence
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from seestar_controller.store.frames import Frame, read_frame_bytes
from seestar_controller.store.project import Project

from .errors import ScanError
from .settings import _check_project

logger = logging.getLogger(__name__)

#: Default full width/height of a star cutout, in pixels.
DEFAULT_CUTOUT_SIZE = 32

#: Default per-frame duration of the blink GIF, in milliseconds.
DEFAULT_BLINK_DURATION_MS = 500

#: Default lower percentile of the finite cutout values (black point).
DEFAULT_LO_PERCENTILE = 0.1

#: Default upper percentile of the finite cutout values (white point).
DEFAULT_HI_PERCENTILE = 99.5

#: RGBA color of the star crosshair drawn on the cutout.
CROSSHAIR_COLOR = (255, 80, 80, 255)

#: Half-length of the crosshair arms, in pixels.
CROSSHAIR_ARM = 5


def tonemap_gray(
    data: Any,
    *,
    lo_percentile: float = DEFAULT_LO_PERCENTILE,
    hi_percentile: float = DEFAULT_HI_PERCENTILE,
) -> np.ndarray:
    """Tone-map a 2-D float image to a uint8 gray image (NaN/inf -> 0).

    The black/white points are the ``lo_percentile`` / ``hi_percentile``
    percentiles of the FINITE values; a degenerate range (``hi <= lo``, e.g.
    a constant cutout) is widened so the mapping never divides by zero.
    Raises :class:`~.errors.ScanError` for a non-2-D input or bad
    percentiles.
    """
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim != 2:
        raise ScanError(
            f"tonemap_gray expects a 2-D (H, W) array, got shape {arr.shape}"
        )
    for name, value in (("lo_percentile", lo_percentile), ("hi_percentile", hi_percentile)):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0.0 <= value <= 100.0
        ):
            raise ScanError(
                f"{name} must be a finite number in [0, 100], got {value!r}"
            )
    finite = np.isfinite(arr)
    values = arr[finite]
    if values.size == 0:
        return np.zeros(arr.shape, dtype=np.uint8)
    lo = float(np.percentile(values, lo_percentile))
    hi = float(np.percentile(values, hi_percentile))
    if hi <= lo:
        hi = lo + 1.0
    scaled = (arr - lo) / (hi - lo)
    scaled = np.where(finite, scaled, 0.0)
    np.clip(scaled, 0.0, 1.0, out=scaled)
    return (scaled * 255.0).astype(np.uint8)


def _validate_cutout_size(cutout_size: Any) -> int:
    if isinstance(cutout_size, bool) or not isinstance(cutout_size, int) or cutout_size < 1:
        raise ScanError(f"cutout_size must be a positive int, got {cutout_size!r}")
    return cutout_size


def _validate_position(x: Any, y: Any) -> tuple[float, float]:
    for name, value in (("x", x), ("y", y)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ScanError(f"{name} must be a number, got {value!r}")
        if not math.isfinite(float(value)):
            raise ScanError(f"{name} must be finite, got {value!r}")
    return float(x), float(y)


def _cutout_origin(x: float, y: float, size: int) -> tuple[int, int]:
    """The (x0, y0) top-left pixel of the ``size``-sized window centered on
    the rounded ``(x, y)`` position (may lie outside the image — the caller
    clips)."""
    half = size // 2
    return round(x) - half, round(y) - half


def extract_cutout(
    data: Any,
    x: Any,
    y: Any,
    *,
    cutout_size: int = DEFAULT_CUTOUT_SIZE,
) -> np.ndarray:
    """The square ``cutout_size`` x ``cutout_size`` pixel window of ``data``
    centered on the star at ``(x, y)`` (x = column, y = row), as a float32
    array. The window is clipped at the image edges and zero-padded, so the
    result always has shape ``(cutout_size, cutout_size)``.

    Raises :class:`~.errors.ScanError` for a non-2-D ``data``, a bad
    ``cutout_size``, or a non-finite ``x`` / ``y``.
    """
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim != 2:
        raise ScanError(
            f"extract_cutout expects a 2-D (H, W) array, got shape {arr.shape}"
        )
    size = _validate_cutout_size(cutout_size)
    cx, cy = _validate_position(x, y)
    x0, y0 = _cutout_origin(cx, cy, size)
    out = np.zeros((size, size), dtype=np.float32)
    sx0, sy0 = max(0, x0), max(0, y0)
    sx1 = min(arr.shape[1], x0 + size)
    sy1 = min(arr.shape[0], y0 + size)
    if sx1 > sx0 and sy1 > sy0:
        out[sy0 - y0 : sy1 - y0, sx0 - x0 : sx1 - x0] = arr[sy0:sy1, sx0:sx1]
    return out


def render_cutout_image(
    data: Any,
    x: Any,
    y: Any,
    *,
    cutout_size: int = DEFAULT_CUTOUT_SIZE,
    lo_percentile: float = DEFAULT_LO_PERCENTILE,
    hi_percentile: float = DEFAULT_HI_PERCENTILE,
) -> Image.Image:
    """Render ONE frame's star cutout as an RGB :class:`PIL.Image.Image`:
    the tone-mapped gray cutout (see :func:`tonemap_gray`) with a red
    crosshair at the star's position inside the cutout.

    Raises :class:`~.errors.ScanError` for bad arguments (see
    :func:`extract_cutout` / :func:`tonemap_gray`).
    """
    size = _validate_cutout_size(cutout_size)
    cx, cy = _validate_position(x, y)
    cutout = extract_cutout(data, cx, cy, cutout_size=size)
    gray = tonemap_gray(cutout, lo_percentile=lo_percentile, hi_percentile=hi_percentile)
    image = Image.fromarray(gray, mode="L").convert("RGB")
    x0, y0 = _cutout_origin(cx, cy, size)
    px, py = cx - x0, cy - y0  # the star position inside the cutout
    draw = ImageDraw.Draw(image)
    arm = CROSSHAIR_ARM
    draw.line((px - arm, py, px + arm, py), fill=CROSSHAIR_COLOR[:3], width=1)
    draw.line((px, py - arm, px, py + arm), fill=CROSSHAIR_COLOR[:3], width=1)
    draw.ellipse((px - 2, py - 2, px + 2, py + 2), outline=CROSSHAIR_COLOR[:3], width=1)
    return image


def _read_frame_data(project: Project, frame: Frame) -> np.ndarray:
    """Read one stored sub-frame's 2-D pixel array (lazy astropy import).
    Raises :class:`~.errors.ScanError` when the frame is unreadable or not
    2-D."""
    from astropy.io import fits as pyfits

    try:
        raw = read_frame_bytes(project, frame)
        with pyfits.open(io.BytesIO(raw), memmap=False) as hdul:
            data = hdul[0].data
    except Exception as exc:
        raise ScanError(
            f"frame {frame.id} of project {project.slug!r} is not a readable "
            f"FITS file: {exc}"
        ) from exc
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim != 2:
        raise ScanError(f"frame {frame.id} is not a 2-D image (shape {arr.shape})")
    return arr


def render_blink_frames(
    project: Any,
    frames: Sequence[Any],
    x: Any,
    y: Any,
    *,
    cutout_size: int = DEFAULT_CUTOUT_SIZE,
    lo_percentile: float = DEFAULT_LO_PERCENTILE,
    hi_percentile: float = DEFAULT_HI_PERCENTILE,
) -> tuple[Image.Image, ...]:
    """Render the cutout of the star at stack pixel ``(x, y)`` across
    ``frames`` (the project's sub-frames, ordered by jd) — the blink frames,
    one RGB :class:`PIL.Image.Image` per READABLE frame, in frame order.

    The subs share the stack's pointing, so the stack pixel position applies
    directly to every sub (the same assumption as
    :func:`~.scan.scan_project`). An unreadable frame is skipped (logged) —
    never a crash.

    Raises :class:`TypeError` for a non-Project ``project`` and
    :class:`~.errors.ScanError` for bad ``x`` / ``y`` / ``cutout_size`` /
    percentile arguments.
    """
    project = _check_project(project)
    cx, cy = _validate_position(x, y)
    size = _validate_cutout_size(cutout_size)
    if not isinstance(frames, Sequence) or isinstance(frames, (str, bytes)):
        raise TypeError(f"frames must be a sequence of Frame records, got {frames!r}")
    images: list[Image.Image] = []
    for frame in frames:
        if not isinstance(frame, Frame):
            raise TypeError(f"frames must be Frame records, got {type(frame).__name__}")
        try:
            data = _read_frame_data(project, frame)
        except Exception as exc:  # unreadable frame: skip, never crash  # noqa: BLE001
            logger.info("blink: skipping unreadable frame: %s", exc)
            continue
        images.append(
            render_cutout_image(
                data,
                cx,
                cy,
                cutout_size=size,
                lo_percentile=lo_percentile,
                hi_percentile=hi_percentile,
            )
        )
    return tuple(images)


def render_blink_gif(
    images: Sequence[Image.Image],
    *,
    duration_ms: int = DEFAULT_BLINK_DURATION_MS,
) -> bytes:
    """Pack the blink ``images`` into ONE animated GIF (bytes): every frame
    converted to palette mode, saved with ``save_all=True``, ``loop=0`` and
    the per-frame ``duration_ms`` — PIL reads it back with ``n_frames ==
    len(images)``.

    Raises :class:`~.errors.ScanError` for an empty image sequence or a bad
    ``duration_ms``, :class:`TypeError` for non-Image items.
    """
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 1:
        raise ScanError(f"duration_ms must be a positive int, got {duration_ms!r}")
    if not isinstance(images, Sequence) or isinstance(images, (str, bytes)):
        raise TypeError(f"images must be a sequence of PIL Images, got {images!r}")
    if not images:
        raise ScanError("render_blink_gif needs at least one image")
    frames: list[Image.Image] = []
    for image in images:
        if not isinstance(image, Image.Image):
            raise TypeError(f"images must be PIL Images, got {type(image).__name__}")
        frames.append(image.convert("P", palette=Image.Palette.ADAPTIVE))
    buffer = io.BytesIO()
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=duration_ms,
        loop=0,
    )
    return buffer.getvalue()


__all__ = [
    "CROSSHAIR_ARM",
    "CROSSHAIR_COLOR",
    "DEFAULT_BLINK_DURATION_MS",
    "DEFAULT_CUTOUT_SIZE",
    "DEFAULT_HI_PERCENTILE",
    "DEFAULT_LO_PERCENTILE",
    "extract_cutout",
    "render_blink_frames",
    "render_blink_gif",
    "render_cutout_image",
    "tonemap_gray",
]
