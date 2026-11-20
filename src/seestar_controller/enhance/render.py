"""Tone mapping + preview rendering — the PURE render layer of
:mod:`seestar_controller.enhance` (image-enhance ticket 01).

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) —
:mod:`numpy` + :mod:`PIL` only, fully unit-testable headless. The widget layer
(:mod:`.viewer`) imports ``gi`` at module top and is never imported by this
module or eagerly by the package ``__init__``.

What lives here:

- :func:`tonemap` — map a float image (``(H, W)`` or ``(H, W, 3)``, NaN
  allowed) to a uint8 image: percentile-based default ``lo``/``hi``
  (:data:`DEFAULT_LO_PERCENTILE` / :data:`DEFAULT_HI_PERCENTILE` of the finite
  values), linear normalization to [0, 1], an optional stretch
  (:data:`STRETCHES` — ``linear`` / ``asinh`` / ``log`` / ``sqrt``), an optional
  ``gamma``, then 8-bit quantization. **NaN/inf pixels map to 0.** For RGB the
  same ``lo``/``hi``/stretch is applied to all channels (global percentiles —
  the color balance is preserved);
- :func:`to_pil_image` — a uint8 array to a :class:`PIL.Image.Image` (mode
  ``L`` or ``RGB``);
- :func:`downscale` — fast mean-pooling downscale to ``max_size`` (pure numpy,
  integer block factor — no interpolation, fast enough for slider
  interaction);
- :func:`render_preview` — the fast preview path: **downscale FIRST, then
  tone-map**, so a 2048² stack renders in well under a second.

No scipy — numpy only (per the ticket's dependency rule).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from PIL import Image

from .fits_io import EnhanceError, FitsImage

#: The available stretch functions.
STRETCHES: tuple[str, ...] = ("linear", "asinh", "log", "sqrt")

#: The default stretch.
DEFAULT_STRETCH = "linear"

#: Default lower percentile for the tone-map range (of the finite values).
DEFAULT_LO_PERCENTILE = 0.1

#: Default upper percentile for the tone-map range (of the finite values).
DEFAULT_HI_PERCENTILE = 99.5

#: Default maximum preview dimension (pixels) — the fast path downscales to
#: this before tone mapping.
DEFAULT_MAX_PREVIEW_SIZE = 1024

#: Softness of the asinh stretch: ``y = asinh(k * x) / asinh(k)`` with this
#: ``k`` (maps [0, 1] -> [0, 1], brightening the midtones).
ASINH_SOFTNESS = 10.0

#: Base of the log stretch: ``y = log(1 + x * (base - 1))`` maps [0, 1] ->
#: [0, 1] (``log(base) = 1``).
LOG_BASE = math.e


def _validate_gamma(gamma: Any) -> float:
    if isinstance(gamma, bool) or not isinstance(gamma, (int, float)):
        raise EnhanceError(f"gamma must be a number, got {gamma!r}")
    value = float(gamma)
    if not math.isfinite(value) or value <= 0.0:
        raise EnhanceError(f"gamma must be a finite number > 0, got {gamma!r}")
    return value


def tonemap(
    data: np.ndarray,
    lo: float | None = None,
    hi: float | None = None,
    stretch: str = DEFAULT_STRETCH,
    gamma: float = 1.0,
) -> np.ndarray:
    """Tone-map a float image to a uint8 image (``(H, W)`` or ``(H, W, 3)``).

    - ``lo`` / ``hi`` — the black/white points; when None they default to the
      :data:`DEFAULT_LO_PERCENTILE` / :data:`DEFAULT_HI_PERCENTILE` percentiles
      of the FINITE values (NaN/inf are excluded from the statistics). A
      degenerate range (``hi <= lo``, e.g. a constant image) is widened to
      ``hi = lo + 1`` so the mapping never divides by zero;
    - ``stretch`` — one of :data:`STRETCHES`, applied to the normalized
      [0, 1] values (all map [0, 1] -> [0, 1], monotonically);
    - ``gamma`` — applied after the stretch (``y ** gamma``; ``gamma < 1``
      brightens);
    - NaN/inf pixels map to **0**.

    Returns a uint8 array of the same shape (``(H, W)`` or ``(H, W, 3)``).
    Raises :class:`EnhanceError` for a bad shape, unknown stretch, or bad
    ``lo``/``hi``/``gamma``.
    """
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim not in (2, 3) or (arr.ndim == 3 and arr.shape[2] != 3):
        raise EnhanceError(
            f"tonemap expects a (H, W) or (H, W, 3) array, got shape {arr.shape}"
        )
    if stretch not in STRETCHES:
        raise EnhanceError(
            f"unknown stretch {stretch!r} — valid stretches are {', '.join(STRETCHES)}"
        )
    gamma = _validate_gamma(gamma)
    for name, value in (("lo", lo), ("hi", hi)):
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise EnhanceError(f"{name} must be a number or None, got {value!r}")
            if not math.isfinite(float(value)):
                raise EnhanceError(f"{name} must be finite, got {value!r}")

    finite = np.isfinite(arr)
    if lo is None or hi is None:
        values = arr[finite]
        if values.size == 0:
            lo = 0.0 if lo is None else float(lo)
            hi = 1.0 if hi is None else float(hi)
        else:
            if lo is None:
                lo = float(np.percentile(values, DEFAULT_LO_PERCENTILE))
            if hi is None:
                hi = float(np.percentile(values, DEFAULT_HI_PERCENTILE))
    lo_f, hi_f = float(lo), float(hi)
    if not hi_f > lo_f:
        hi_f = lo_f + 1.0  # degenerate range (e.g. constant image)

    x = (arr - np.float32(lo_f)) / np.float32(hi_f - lo_f)
    x = np.clip(x, 0.0, 1.0)  # NaN stays NaN through clip
    if stretch == "linear":
        y = x
    elif stretch == "sqrt":
        y = np.sqrt(x)
    elif stretch == "log":
        y = np.log1p(x * np.float32(LOG_BASE - 1.0))
    else:  # asinh
        k = np.float32(ASINH_SOFTNESS)
        y = np.arcsinh(x * k) / np.arcsinh(k)
    if gamma != 1.0:
        y = np.power(y, np.float32(gamma))
    y = np.where(finite, y, np.float32(0.0))  # NaN/inf -> 0
    return np.rint(y * np.float32(255.0)).astype(np.uint8)


def to_pil_image(arr: np.ndarray) -> Image.Image:
    """Convert a uint8 array to a :class:`PIL.Image.Image` — mode ``L`` for
    ``(H, W)``, ``RGB`` for ``(H, W, 3)``.

    Raises :class:`EnhanceError` for a non-uint8 array or a bad shape.
    """
    a = np.asarray(arr)
    if a.dtype != np.uint8:
        raise EnhanceError(f"to_pil_image expects a uint8 array, got {a.dtype}")
    if a.ndim == 2:
        return Image.fromarray(np.ascontiguousarray(a), mode="L")
    if a.ndim == 3 and a.shape[2] == 3:
        return Image.fromarray(np.ascontiguousarray(a), mode="RGB")
    raise EnhanceError(
        f"to_pil_image expects a (H, W) or (H, W, 3) uint8 array, got shape {a.shape}"
    )


def downscale(data: np.ndarray, max_size: int = DEFAULT_MAX_PREVIEW_SIZE) -> np.ndarray:
    """Downscale ``data`` so its largest dimension is at most ``max_size``,
    by integer-factor mean pooling (pure numpy — fast, no interpolation).

    Images already within ``max_size`` are returned unchanged. Returns a float
    array of the same number of dimensions (``(h, w)`` or ``(h, w, 3)``).
    """
    arr = np.asarray(data)
    if arr.ndim not in (2, 3):
        raise EnhanceError(f"downscale expects a 2-D or 3-D array, got {arr.shape}")
    if max_size is None or max_size <= 0:
        return arr
    height, width = int(arr.shape[0]), int(arr.shape[1])
    if max(height, width) <= max_size:
        return arr
    factor = int(math.ceil(max(height, width) / max_size))
    h2 = (height // factor) * factor
    w2 = (width // factor) * factor
    if h2 == 0 or w2 == 0:
        return arr  # degenerate tiny image: leave as-is
    cropped = arr[:h2, :w2]
    if arr.ndim == 2:
        return cropped.reshape(h2 // factor, factor, w2 // factor, factor).mean(axis=(1, 3))
    return cropped.reshape(h2 // factor, factor, w2 // factor, factor, arr.shape[2]).mean(
        axis=(1, 3)
    )


def render_preview(
    fits_image: FitsImage,
    max_size: int = DEFAULT_MAX_PREVIEW_SIZE,
    *,
    lo: float | None = None,
    hi: float | None = None,
    stretch: str = DEFAULT_STRETCH,
    gamma: float = 1.0,
) -> Image.Image:
    """Render a tone-mapped preview of ``fits_image`` as a PIL image.

    The FAST PATH: the image is downscaled to ``max_size`` FIRST (see
    :func:`downscale`), then tone-mapped (see :func:`tonemap`) — so even a
    2048² stack renders in a fraction of a second, keeping slider interaction
    smooth (ticket 02 will re-render on every parameter change).
    """
    data = downscale(fits_image.data, max_size)
    return to_pil_image(tonemap(data, lo=lo, hi=hi, stretch=stretch, gamma=gamma))
