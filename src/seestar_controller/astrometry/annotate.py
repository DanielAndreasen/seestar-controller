"""Annotated-stack rendering — the PURE render layer of astrometry ticket 03
(:mod:`seestar_controller.astrometry.annotate`).

This is a PURE module of :mod:`seestar_controller.astrometry`: **no ``gi``
import** (the sandbox has no GTK, see docs/wiki/assumptions.md; a subprocess
test enforces the boundary) — stdlib + :mod:`numpy` + :mod:`PIL` + the pure
:mod:`seestar_controller.gaia` (the tone-map + overlay constants, reused —
NOT :mod:`seestar_controller.enhance`, the module boundary forbids it) +
:mod:`seestar_controller.store` + the sibling pure modules only. astropy
enters lazily inside :func:`fov_corner_sky` (via :func:`~.wcs.cards_to_wcs`)
so ``import seestar_controller.astrometry`` stays astropy-free.

What lives here — the annotated PNG export of a stack (issue 03a), rendered
over the tone-mapped image with the SAME PIL path as the gaia overlay
(:func:`seestar_controller.gaia.overlay.render_stack_overlay`):

- :func:`render_annotated_stack` — the stack tone-mapped to RGB with
  - every MATCHED cross-match row ringed + labelled with its Gaia G
    magnitude (``G 12.34``), every UNMATCHED row flagged with a distinct X
    marker (the gaia-crossmatch 03 overlay rendering, reused);
  - the **FOV rectangle** — the frame's field of view drawn as a rectangle
    inset a few pixels from the image border, with the corner sky
    coordinates (RA/Dec) labelled when the header carries a celestial WCS;
  - a **scale bar** — a bar of a "nice" length (1″…2°) that fits in a
    quarter of the image width, labelled (``5′`` / ``1°``), drawn when the
    header carries a pixel scale;
- :func:`annotated_png_bytes` — the same render as PNG file bytes;
- :func:`fov_corner_sky` / :func:`scale_bar_arcsec` — the pure helpers (the
  corner projection and the nice-length choice), unit-tested headless.

The image is downscaled to at most ``max_size`` on its long side BEFORE the
annotations are drawn (annotations stay crisp; the row coordinates and the
FOV corners are scaled accordingly) — the same choice as the gaia overlay.
"""

from __future__ import annotations

import io
import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from seestar_controller.gaia.overlay import (
    DEFAULT_HI_PERCENTILE,
    DEFAULT_LO_PERCENTILE,
    DEFAULT_MAX_OVERLAY_SIZE,
    LINE_WIDTH,
    MATCHED_COLOR,
    POINT_RADIUS,
    RING_RADIUS,
    UNMATCHED_COLOR,
    tonemap_gray,
)

from .errors import ExportError
from .wcs import cards_to_wcs, has_celestial_wcs, header_pixel_scale_arcsec

#: RGBA color of the FOV rectangle + the corner coordinate labels.
FOV_COLOR = (255, 214, 90, 255)

#: RGBA color of the scale bar + its label.
SCALE_BAR_COLOR = (120, 200, 255, 255)

#: Inset of the FOV rectangle from the image border, in (unscaled) pixels.
FOV_MARGIN_PX = 6.0

#: Width of the FOV rectangle outline, in pixels.
FOV_LINE_WIDTH = 2

#: The scale bar never exceeds this fraction of the image width.
SCALE_BAR_MAX_FRACTION = 0.25

#: Margin of the scale bar from the bottom-left corner, in pixels.
SCALE_BAR_MARGIN_PX = 14.0

#: Height of the scale bar line, in pixels.
SCALE_BAR_HEIGHT_PX = 4.0

#: "Nice" scale-bar lengths in arcseconds (1″ … 2°), ascending.
NICE_SCALE_BAR_ARCSEC: tuple[float, ...] = (
    1.0,
    2.0,
    5.0,
    10.0,
    15.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
    900.0,
    1800.0,
    3600.0,
    7200.0,
)

#: Arcseconds per degree / per arcminute.
ARCSEC_PER_DEG = 3600.0
ARCSEC_PER_ARCMIN = 60.0


def _gray(data: Any) -> np.ndarray:
    """A 2-D float32 view of image data (RGB ``(H, W, 3)`` -> luminance)."""
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim == 3:
        arr = arr.mean(axis=2)
    if arr.ndim != 2:
        raise ExportError(f"stack image must be 2-D (or RGB), got shape {arr.shape}")
    return arr


def _check_percentile(value: Any, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 <= value <= 100.0
    ):
        raise ExportError(f"{name} must be a finite number in [0, 100], got {value!r}")
    return float(value)


def _check_max_size(max_size: Any) -> int:
    if isinstance(max_size, bool) or not isinstance(max_size, int) or max_size < 1:
        raise ExportError(f"max_size must be a positive int, got {max_size!r}")
    return max_size


def _row_fields(row: Any) -> tuple[float, float, bool, float | None]:
    """The annotation fields of one cross-match row (duck-typed: anything
    with numeric ``x``/``y``, a boolean ``matched`` and — when matched — a
    finite ``g`` for the label, e.g. a
    :class:`~seestar_controller.gaia.matching.CrossmatchRow` or a
    :class:`~seestar_controller.store.crossmatch.CrossmatchRecord`)."""
    x = getattr(row, "x", None)
    y = getattr(row, "y", None)
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise ExportError(f"row.x must be a finite number, got {x!r}")
    if isinstance(y, bool) or not isinstance(y, (int, float)) or not math.isfinite(y):
        raise ExportError(f"row.y must be a finite number, got {y!r}")
    matched = getattr(row, "matched", None)
    if not isinstance(matched, bool):
        raise ExportError(f"row.matched must be a bool, got {matched!r}")
    g: float | None = None
    if matched:
        g = getattr(row, "g", None)
        if isinstance(g, bool) or not isinstance(g, (int, float)) or not math.isfinite(g):
            raise ExportError(f"row.g of a matched row (the G-mag label) must be a finite number, got {g!r}")
    return float(x), float(y), matched, g


def fov_corner_sky(
    header: Mapping[str, Any] | None, shape: tuple[int, int]
) -> tuple[tuple[float, float], ...] | None:
    """The J2000 sky positions ``(ra_deg, dec_deg)`` of the four image
    corners (0-based pixels ``(0, 0)``, ``(nx-1, 0)``, ``(nx-1, ny-1)``,
    ``(0, ny-1)``), or None when ``header`` carries no celestial WCS.

    astropy enters lazily here (via :func:`~.wcs.cards_to_wcs`), so the
    module stays astropy-free at import time. Raises :class:`ExportError`
    for a bad ``shape``.
    """
    if (
        not isinstance(shape, (tuple, list))
        or len(shape) != 2
        or isinstance(shape[0], bool)
        or not isinstance(shape[0], int)
        or isinstance(shape[1], bool)
        or not isinstance(shape[1], int)
        or shape[0] < 1
        or shape[1] < 1
    ):
        raise ExportError(f"shape must be a (ny, nx) pair of positive ints, got {shape!r}")
    if header is None or not has_celestial_wcs(header):
        return None
    wcs = cards_to_wcs(header)
    ny, nx = int(shape[0]), int(shape[1])
    corners = ((0.0, 0.0), (nx - 1.0, 0.0), (nx - 1.0, ny - 1.0), (0.0, ny - 1.0))
    result: list[tuple[float, float]] = []
    for x, y in corners:
        sky = wcs.pixel_to_world(x, y)
        result.append((float(sky.ra.deg) % 360.0, float(sky.dec.deg)))
    return tuple(result)


def scale_bar_arcsec(pixel_scale_arcsec: float, width_px: int) -> float | None:
    """A "nice" scale-bar length in arcseconds for an image ``width_px``
    wide at ``pixel_scale_arcsec`` arcsec/px: the largest of
    :data:`NICE_SCALE_BAR_ARCSEC` whose bar is at most
    :data:`SCALE_BAR_MAX_FRACTION` of the width, or None when no nice
    length fits (or the inputs are invalid)."""
    if (
        isinstance(pixel_scale_arcsec, bool)
        or not isinstance(pixel_scale_arcsec, (int, float))
        or not math.isfinite(pixel_scale_arcsec)
        or pixel_scale_arcsec <= 0
    ):
        return None
    if isinstance(width_px, bool) or not isinstance(width_px, int) or width_px < 1:
        return None
    max_arcsec = pixel_scale_arcsec * width_px * SCALE_BAR_MAX_FRACTION
    chosen: float | None = None
    for candidate in NICE_SCALE_BAR_ARCSEC:
        if candidate <= max_arcsec:
            chosen = candidate
    return chosen


def format_scale_bar(length_arcsec: float) -> str:
    """The scale-bar label for ``length_arcsec``: ``1°`` / ``5′`` / ``30″``."""
    if length_arcsec >= ARCSEC_PER_DEG:
        return f"{length_arcsec / ARCSEC_PER_DEG:g}°"
    if length_arcsec >= ARCSEC_PER_ARCMIN:
        return f"{length_arcsec / ARCSEC_PER_ARCMIN:g}′"
    return f"{length_arcsec:g}″"


def _format_corner(ra_deg: float, dec_deg: float) -> str:
    return f"RA {ra_deg:.3f}° Dec {dec_deg:+.3f}°"


def render_annotated_stack(
    image_data: Any,
    rows: Sequence[Any] = (),
    *,
    header: Mapping[str, Any] | None = None,
    lo_percentile: float = DEFAULT_LO_PERCENTILE,
    hi_percentile: float = DEFAULT_HI_PERCENTILE,
    max_size: int = DEFAULT_MAX_OVERLAY_SIZE,
) -> Image.Image:
    """Render the stack as an RGB PIL image with the annotations drawn on.

    - ``image_data`` — the stack's pixel array (2-D float, NaN allowed; an
      RGB ``(H, W, 3)`` array is converted to luminance);
    - ``rows`` — the cross-match rows (duck-typed, see :func:`_row_fields`):
      every MATCHED row is drawn as a filled point + ring and labelled with
      its Gaia G magnitude (``G 12.34``); every UNMATCHED row is drawn as a
      distinct X marker;
    - ``header`` — the stack's FITS header as a plain dict (None = none):
      when it carries a celestial WCS the FOV rectangle's corners are
      labelled with their sky coordinates; when it carries a pixel scale a
      scale bar is drawn;
    - ``lo_percentile`` / ``hi_percentile`` — the tone-map black/white
      points (percentiles of the finite values);
    - ``max_size`` — the image is downscaled to at most this many pixels on
      its long side BEFORE the annotations are drawn.

    Returns an RGB :class:`PIL.Image.Image`. Raises :class:`ExportError` for
    a bad image, bad percentiles, a bad ``max_size``, or a malformed row.
    """
    max_size = _check_max_size(max_size)
    lo_percentile = _check_percentile(lo_percentile, "lo_percentile")
    hi_percentile = _check_percentile(hi_percentile, "hi_percentile")
    if header is not None and not isinstance(header, Mapping):
        raise TypeError(f"header must be a mapping of FITS cards or None, got {header!r}")
    cards = dict(header) if header is not None else {}
    gray = tonemap_gray(
        _gray(image_data), lo_percentile=lo_percentile, hi_percentile=hi_percentile
    )
    image = Image.fromarray(gray, mode="L").convert("RGB")
    scale = 1.0
    if max(image.size) > max_size:
        scale = max_size / float(max(image.size))
        new_size = (
            max(1, round(image.width * scale)),
            max(1, round(image.height * scale)),
        )
        image = image.resize(new_size, Image.Resampling.BILINEAR)
    draw = ImageDraw.Draw(image)

    # the cross-match rows (the gaia-crossmatch 03 overlay rendering)
    for row in rows:
        x, y, matched, g = _row_fields(row)
        px, py = x * scale, y * scale
        if matched:
            r = POINT_RADIUS * scale
            draw.ellipse((px - r, py - r, px + r, py + r), fill=MATCHED_COLOR)
            rr = RING_RADIUS * scale
            draw.ellipse((px - rr, py - rr, px + rr, py + rr), outline=MATCHED_COLOR, width=LINE_WIDTH)
            draw.text(
                ((px + rr + 2.0), (py - rr - 2.0)),
                f"G {g:.2f}",
                fill=MATCHED_COLOR,
            )
        else:
            rr = RING_RADIUS * scale
            draw.line((px - rr, py - rr, px + rr, py + rr), fill=UNMATCHED_COLOR, width=LINE_WIDTH)
            draw.line((px - rr, py + rr, px + rr, py - rr), fill=UNMATCHED_COLOR, width=LINE_WIDTH)

    # the FOV rectangle (the frame's field of view) + corner sky labels
    w, h = image.size
    m = FOV_MARGIN_PX * scale
    draw.rectangle(
        (m, m, w - 1 - m, h - 1 - m), outline=FOV_COLOR, width=FOV_LINE_WIDTH
    )
    corners = fov_corner_sky(cards, (int(gray.shape[0]), int(gray.shape[1])))
    if corners is not None:
        # corner order: TL, TR, BR, BL (original pixel coords, scaled)
        positions = (
            (m + 4.0, m + 4.0, corners[0], 0),
            (w - 1 - m - 4.0, m + 4.0, corners[1], 1),
            (w - 1 - m - 4.0, h - 1 - m - 12.0, corners[2], 1),
            (m + 4.0, h - 1 - m - 12.0, corners[3], 0),
        )
        for cx, cy, (ra, dec), align_right in positions:
            label = _format_corner(ra, dec)
            if align_right:
                draw.text((cx, cy), label, fill=FOV_COLOR, anchor="ra")
            else:
                draw.text((cx, cy), label, fill=FOV_COLOR, anchor="la")

    # the scale bar (bottom-left) when the header carries a pixel scale
    pixel_scale = header_pixel_scale_arcsec(cards)
    if pixel_scale is not None:
        length_arcsec = scale_bar_arcsec(pixel_scale, w)
        if length_arcsec is not None:
            bar_px = length_arcsec / pixel_scale * scale
            x0 = SCALE_BAR_MARGIN_PX * scale
            y0 = h - SCALE_BAR_MARGIN_PX * scale
            draw.line(
                (x0, y0, x0 + bar_px, y0), fill=SCALE_BAR_COLOR, width=max(1, round(SCALE_BAR_HEIGHT_PX * scale))
            )
            draw.line((x0, y0 - 3.0, x0, y0 + 3.0), fill=SCALE_BAR_COLOR, width=LINE_WIDTH)
            draw.line(
                (x0 + bar_px, y0 - 3.0, x0 + bar_px, y0 + 3.0), fill=SCALE_BAR_COLOR, width=LINE_WIDTH
            )
            draw.text(
                (x0 + bar_px / 2.0, y0 - 12.0),
                format_scale_bar(length_arcsec),
                fill=SCALE_BAR_COLOR,
                anchor="ma",
            )
    return image


def annotated_png_bytes(
    image_data: Any,
    rows: Sequence[Any] = (),
    *,
    header: Mapping[str, Any] | None = None,
    lo_percentile: float = DEFAULT_LO_PERCENTILE,
    hi_percentile: float = DEFAULT_HI_PERCENTILE,
    max_size: int = DEFAULT_MAX_OVERLAY_SIZE,
) -> bytes:
    """Render :func:`render_annotated_stack` and return the PNG file bytes."""
    image = render_annotated_stack(
        image_data,
        rows,
        header=header,
        lo_percentile=lo_percentile,
        hi_percentile=hi_percentile,
        max_size=max_size,
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


__all__ = [
    "ARCSEC_PER_ARCMIN",
    "ARCSEC_PER_DEG",
    "FOV_COLOR",
    "FOV_LINE_WIDTH",
    "FOV_MARGIN_PX",
    "NICE_SCALE_BAR_ARCSEC",
    "SCALE_BAR_COLOR",
    "SCALE_BAR_HEIGHT_PX",
    "SCALE_BAR_MARGIN_PX",
    "SCALE_BAR_MAX_FRACTION",
    "annotated_png_bytes",
    "format_scale_bar",
    "fov_corner_sky",
    "render_annotated_stack",
    "scale_bar_arcsec",
]
