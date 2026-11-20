"""The composable enhancement operations — the PURE op layer of
:mod:`seestar_controller.enhance` (image-enhance ticket 02).

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) — stdlib +
:mod:`numpy` only (no scipy, per the ticket's dependency rule). The widget
layer (:mod:`.viewer`) imports ``gi`` at module top and is never imported by
this module or eagerly by the package ``__init__``.

Every op is a **pure function** ``apply_*(img, params) -> img``:

- the input is a float array ``(H, W)`` grayscale or ``(H, W, 3)`` RGB (any
  numeric dtype is accepted and converted to float32); the input is NEVER
  mutated (each op works on a copy) and the same input always gives the same
  output;
- **NaN-safe, mask-preserving**: pixels that are NaN in the input are NaN in
  the output (the NaN mask is carried through every op); all statistics
  (min/max, means, medians, the polynomial fit) are computed over the FINITE
  values only; the gaussian blur fills NaN with the finite mean before
  convolving and the mask is restored afterwards;
- each op has a frozen params dataclass that validates its ranges in
  ``__post_init__`` (raising :class:`~.fits_io.EnhanceError`) and serializes
  to/from a plain dict (:meth:`to_dict` / :meth:`from_dict`, for ticket 04's
  per-image persistence).

The ops (see :data:`OPS` for the name -> spec registry used by
:mod:`.pipeline`):

- :func:`apply_stretch` / :class:`StretchParams` — linear min-max normalize
  to [0, 1], asinh stretch with a softening parameter ``sigma``
  (``asinh(sigma*x)/asinh(sigma)``), or histogram equalization (numpy
  histogram + cdf). All three are monotonic non-decreasing (ordering is
  preserved);
- :func:`apply_denoise` / :class:`DenoiseParams` — gaussian blur via a numpy
  separable convolution (small gaussian kernel + edge padding), blended:
  ``out = (1 - strength) * img + strength * blur``;
- :func:`apply_sharpen` / :class:`SharpenParams` — unsharp mask:
  ``out = img + amount * (img - blur)`` with the same gaussian blur;
- :func:`apply_white_balance` / :class:`WhiteBalanceParams` — gray-world
  (per-channel finite mean scaled to the global mean) or explicit per-channel
  ``gains``; RGB only, grayscale is a no-op;
- :func:`apply_background` / :class:`BackgroundParams` — estimate the
  background (per-block median grid, or a polynomial fit of degree ``degree``
  to the downsampled block means) and subtract it.

Pipeline ops work on FLOAT data (typically the raw :class:`~.fits_io.FitsImage`
data); tone mapping for display stays in :mod:`.render`.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from .fits_io import EnhanceError

#: The available stretch kinds.
STRETCH_KINDS: tuple[str, ...] = ("linear", "asinh", "histogram")

#: The available white-balance modes.
WB_MODES: tuple[str, ...] = ("gray-world", "gains")

#: The available background-estimation methods.
BACKGROUND_METHODS: tuple[str, ...] = ("median", "poly")

#: Default histogram-equalization bin count.
DEFAULT_HISTOGRAM_BINS = 256

#: Block size divisor for the background grid: the image is split into cells
#: of about ``min(H, W) // BACKGROUND_GRID_DIVISOR`` pixels (at least
#: :data:`BACKGROUND_MIN_CELL`).
BACKGROUND_GRID_DIVISOR = 8
BACKGROUND_MIN_CELL = 4


def _as_image(img: np.ndarray) -> np.ndarray:
    """Validate ``img`` as a float32 ``(H, W)`` / ``(H, W, 3)`` image and
    return a float32 COPY (the input is never mutated)."""
    try:
        arr = np.array(img, dtype=np.float32, copy=True)
    except (TypeError, ValueError) as exc:
        raise EnhanceError(f"enhancement ops expect a numeric image array: {exc}") from exc
    if arr.ndim not in (2, 3) or (arr.ndim == 3 and arr.shape[2] != 3):
        raise EnhanceError(
            f"enhancement ops expect a (H, W) or (H, W, 3) array, got shape {arr.shape}"
        )
    return arr


def _finite_values(arr: np.ndarray) -> np.ndarray:
    """The finite values of ``arr`` (empty array when all-NaN)."""
    return arr[np.isfinite(arr)]


def _restore_nan_mask(out: np.ndarray, original: np.ndarray) -> np.ndarray:
    """Return ``out`` (float32) with the NaN mask of ``original`` re-applied
    (NaN in -> NaN out)."""
    out = np.ascontiguousarray(out, dtype=np.float32)
    out[np.isnan(original)] = np.nan
    return out


def _number(value: Any, name: str) -> float:
    """A finite float, rejecting bools and non-numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EnhanceError(f"{name} must be a number, got {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise EnhanceError(f"{name} must be finite, got {value!r}")
    return result


def _int(value: Any, name: str) -> int:
    """An int, rejecting bools and non-ints."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise EnhanceError(f"{name} must be an int, got {value!r}")
    return int(value)


class _SerializableParams:
    """Mixin giving a params dataclass dict serialization
    (:meth:`to_dict` / :meth:`from_dict`) for ticket 04's persistence."""

    def to_dict(self) -> dict[str, Any]:
        """The params as a plain JSON-able dict (field name -> value)."""
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> _SerializableParams:
        """Rebuild the params from :meth:`to_dict` output. Unknown keys,
        wrong types, or out-of-range values raise :class:`EnhanceError`."""
        if not isinstance(data, Mapping):
            raise EnhanceError(
                f"{cls.__name__}.from_dict expects a dict, got {data!r}"
            )
        fields = {f.name for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
        unknown = sorted(set(data) - fields)
        if unknown:
            raise EnhanceError(
                f"{cls.__name__}.from_dict got unknown keys {unknown} "
                f"(valid: {sorted(fields)})"
            )
        kwargs: dict[str, Any] = dict(data)
        if "gains" in kwargs:  # JSON turns the tuple into a list
            try:
                kwargs["gains"] = tuple(kwargs["gains"])
            except TypeError as exc:
                raise EnhanceError(f"gains must be a sequence of 3 numbers: {exc}") from exc
        try:
            return cls(**kwargs)
        except TypeError as exc:
            raise EnhanceError(f"{cls.__name__}.from_dict: {exc}") from exc


# --- stretch ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class StretchParams(_SerializableParams):
    """Parameters of :func:`apply_stretch`.

    - ``kind`` — one of :data:`STRETCH_KINDS` (``linear`` / ``asinh`` /
      ``histogram``);
    - ``sigma`` — the asinh softening parameter (``asinh(sigma*x)/asinh(sigma)``
      on the [0, 1]-normalized values; larger = stronger midtone lift);
    - ``bins`` — the histogram-equalization bin count.
    """

    kind: str = "asinh"
    sigma: float = 10.0
    bins: int = DEFAULT_HISTOGRAM_BINS

    def __post_init__(self) -> None:
        if self.kind not in STRETCH_KINDS:
            raise EnhanceError(
                f"unknown stretch kind {self.kind!r} — valid kinds are "
                f"{', '.join(STRETCH_KINDS)}"
            )
        sigma = _number(self.sigma, "sigma")
        if sigma <= 0.0:
            raise EnhanceError(f"sigma must be > 0, got {self.sigma!r}")
        bins = _int(self.bins, "bins")
        if not 2 <= bins <= 65536:
            raise EnhanceError(f"bins must be in [2, 65536], got {self.bins!r}")


def apply_stretch(img: np.ndarray, params: StretchParams) -> np.ndarray:
    """Stretch ``img`` to [0, 1] (float32, NaN mask preserved).

    The finite values are min-max normalized to [0, 1] first, then:

    - ``linear`` — identity on the normalized values;
    - ``asinh`` — ``asinh(sigma * x) / asinh(sigma)`` (monotonic, brightens
      the midtones);
    - ``histogram`` — histogram equalization: a numpy histogram of the
      finite values (+ cdf) maps each pixel through the normalized cdf.

    All three kinds are monotonic non-decreasing, so the pixel ORDERING is
    preserved. A degenerate range (constant image, ``hi <= lo``) maps all
    finite pixels to 0. An all-NaN image is returned unchanged (all NaN).
    """
    if not isinstance(params, StretchParams):
        raise EnhanceError(f"apply_stretch expects StretchParams, got {params!r}")
    arr = _as_image(img)
    vals = _finite_values(arr)
    if vals.size == 0:
        return arr  # all-NaN: nothing to stretch
    lo = float(vals.min())
    hi = float(vals.max())
    if not hi > lo:
        out = np.zeros(arr.shape, dtype=np.float32)  # constant image -> 0
        return _restore_nan_mask(out, arr)
    x = np.clip((arr - np.float32(lo)) / np.float32(hi - lo), 0.0, 1.0)  # NaN stays NaN
    if params.kind == "linear":
        y = x
    elif params.kind == "asinh":
        sigma = np.float32(params.sigma)
        y = np.arcsinh(x * sigma) / np.arcsinh(sigma)
    else:  # histogram equalization via histogram + cdf
        hist, edges = np.histogram(vals, bins=params.bins, range=(lo, hi))
        cdf = np.cumsum(hist).astype(np.float64)
        total = cdf[-1] - cdf[0]
        norm = (cdf - cdf[0]) / total if total > 0 else cdf * 0.0
        idx = np.searchsorted(edges, arr, side="right") - 1
        idx = np.clip(idx, 0, params.bins - 1)
        y = norm[idx].astype(np.float32)
    return _restore_nan_mask(y, arr)


# --- gaussian blur (shared by denoise + sharpen) ----------------------------------------------


def _gaussian_kernel1d(radius: int) -> np.ndarray:
    """A normalized 1-D gaussian kernel of half-width ``radius``
    (sigma = radius), float64."""
    x = np.arange(-radius, radius + 1, dtype=np.float64)
    kernel = np.exp(-0.5 * (x / float(radius)) ** 2)
    return kernel / kernel.sum()


def _convolve_axis(arr: np.ndarray, kernel: np.ndarray, axis: int) -> np.ndarray:
    """Separable 1-D convolution of ``arr`` along ``axis`` with ``kernel``,
    edge-padded (np.pad mode='edge') — pure numpy, O(kernel) sliced adds."""
    radius = kernel.size // 2
    pad = [(0, 0)] * arr.ndim
    pad[axis] = (radius, radius)
    padded = np.pad(arr, pad, mode="edge")
    out = np.zeros(arr.shape, dtype=np.float64)
    for j in range(kernel.size):
        sl = [slice(None)] * arr.ndim
        sl[axis] = slice(j, j + arr.shape[axis])
        out += kernel[j] * padded[tuple(sl)]
    return out


def _gaussian_blur(arr: np.ndarray, radius: int) -> np.ndarray:
    """Gaussian-blur ``arr`` (float32 (H, W) or (H, W, 3)) with the separable
    kernel of half-width ``radius``. NaN pixels are filled with the finite
    mean before convolving (the caller restores the NaN mask)."""
    vals = _finite_values(arr)
    fill = float(vals.mean()) if vals.size else 0.0
    filled = np.where(np.isfinite(arr), arr, np.float32(fill)).astype(np.float64)
    kernel = _gaussian_kernel1d(radius)
    out = _convolve_axis(filled, kernel, axis=0)
    out = _convolve_axis(out, kernel, axis=1)
    return out


# --- denoise -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class DenoiseParams(_SerializableParams):
    """Parameters of :func:`apply_denoise`.

    - ``strength`` — blend between the original and the blurred image,
      ``out = (1 - strength) * img + strength * blur``, in [0, 1]
      (0 = no-op, 1 = full blur);
    - ``radius`` — the gaussian kernel half-width in pixels (>= 1).
    """

    strength: float = 0.5
    radius: int = 1

    def __post_init__(self) -> None:
        strength = _number(self.strength, "strength")
        if not 0.0 <= strength <= 1.0:
            raise EnhanceError(f"strength must be in [0, 1], got {self.strength!r}")
        radius = _int(self.radius, "radius")
        if not 1 <= radius <= 64:
            raise EnhanceError(f"radius must be an int in [1, 64], got {self.radius!r}")


def apply_denoise(img: np.ndarray, params: DenoiseParams) -> np.ndarray:
    """Denoise ``img`` by blending it with its gaussian blur:
    ``out = (1 - strength) * img + strength * blur`` (float32, NaN mask
    preserved). Reduces zero-mean noise while keeping the mean; ``strength=0``
    is the identity."""
    if not isinstance(params, DenoiseParams):
        raise EnhanceError(f"apply_denoise expects DenoiseParams, got {params!r}")
    arr = _as_image(img)
    blur = _gaussian_blur(arr, params.radius)
    out = (1.0 - params.strength) * arr.astype(np.float64) + params.strength * blur
    return _restore_nan_mask(out, arr)


# --- sharpen (unsharp mask) ---------------------------------------------------------------------


@dataclass(frozen=True)
class SharpenParams(_SerializableParams):
    """Parameters of :func:`apply_sharpen` (unsharp mask).

    - ``amount`` — how much of the high-frequency detail is added back,
      ``out = img + amount * (img - blur)``, in [0, 3] (0 = no-op);
    - ``radius`` — the gaussian kernel half-width of the blur, in pixels.
    """

    amount: float = 1.0
    radius: int = 1

    def __post_init__(self) -> None:
        amount = _number(self.amount, "amount")
        if not 0.0 <= amount <= 3.0:
            raise EnhanceError(f"amount must be in [0, 3], got {self.amount!r}")
        radius = _int(self.radius, "radius")
        if not 1 <= radius <= 64:
            raise EnhanceError(f"radius must be an int in [1, 64], got {self.radius!r}")


def apply_sharpen(img: np.ndarray, params: SharpenParams) -> np.ndarray:
    """Sharpen ``img`` with an unsharp mask: ``out = img + amount * (img -
    blur)`` where ``blur`` is the gaussian blur of half-width ``radius``
    (float32, NaN mask preserved). Increases the gradient magnitude at
    edges; ``amount=0`` is the identity."""
    if not isinstance(params, SharpenParams):
        raise EnhanceError(f"apply_sharpen expects SharpenParams, got {params!r}")
    arr = _as_image(img)
    blur = _gaussian_blur(arr, params.radius)
    out = arr.astype(np.float64) + params.amount * (arr.astype(np.float64) - blur)
    return _restore_nan_mask(out, arr)


# --- white balance ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class WhiteBalanceParams(_SerializableParams):
    """Parameters of :func:`apply_white_balance` (RGB only).

    - ``mode`` — ``gray-world`` (per-channel finite mean scaled to the global
      mean) or ``gains`` (explicit per-channel ``gains``);
    - ``gains`` — the explicit ``(r, g, b)`` gains, each a finite number > 0
      (used in ``gains`` mode; validated always).
    """

    mode: str = "gray-world"
    gains: tuple[float, float, float] = (1.0, 1.0, 1.0)

    def __post_init__(self) -> None:
        if self.mode not in WB_MODES:
            raise EnhanceError(
                f"unknown white-balance mode {self.mode!r} — valid modes are "
                f"{', '.join(WB_MODES)}"
            )
        gains = self.gains
        if isinstance(gains, (str, bytes)) or not isinstance(gains, (tuple, list)):
            raise EnhanceError(f"gains must be a (r, g, b) sequence, got {gains!r}")
        if len(gains) != 3:
            raise EnhanceError(f"gains must have exactly 3 entries, got {gains!r}")
        for i, g in enumerate(gains):
            value = _number(g, f"gains[{i}]")
            if value <= 0.0:
                raise EnhanceError(f"gains[{i}] must be > 0, got {g!r}")
        object.__setattr__(self, "gains", tuple(float(g) for g in gains))


def apply_white_balance(img: np.ndarray, params: WhiteBalanceParams) -> np.ndarray:
    """White-balance an RGB ``img`` (float32, NaN mask preserved).

    - ``gray-world`` — each channel's finite mean is scaled to the global
      mean (the mean of the three channel means), neutralizing a color cast;
    - ``gains`` — the explicit per-channel ``params.gains`` are applied.

    Grayscale input is a NO-OP (a copy is returned unchanged).
    """
    if not isinstance(params, WhiteBalanceParams):
        raise EnhanceError(f"apply_white_balance expects WhiteBalanceParams, got {params!r}")
    arr = _as_image(img)
    if arr.ndim == 2:
        return arr  # grayscale: no-op (already a copy)
    if params.mode == "gains":
        gains = np.asarray(params.gains, dtype=np.float64)
    else:  # gray-world
        channel_means = np.array(
            [
                float(_finite_values(arr[..., c]).mean())
                if np.isfinite(arr[..., c]).any()
                else 0.0
                for c in range(3)
            ],
            dtype=np.float64,
        )
        global_mean = float(channel_means.mean())
        gains = np.array(
            [global_mean / m if m > 0.0 else 1.0 for m in channel_means], dtype=np.float64
        )
    out = arr.astype(np.float64) * gains.reshape(1, 1, 3)
    return _restore_nan_mask(out, arr)


# --- background subtraction -------------------------------------------------------------------------


@dataclass(frozen=True)
class BackgroundParams(_SerializableParams):
    """Parameters of :func:`apply_background`.

    - ``method`` — ``median`` (per-block median grid, nearest-upsampled) or
      ``poly`` (polynomial surface of degree ``degree`` fitted to the
      downsampled block means);
    - ``degree`` — the polynomial degree for ``poly`` (int in [0, 10]).
    """

    method: str = "median"
    degree: int = 2

    def __post_init__(self) -> None:
        if self.method not in BACKGROUND_METHODS:
            raise EnhanceError(
                f"unknown background method {self.method!r} — valid methods are "
                f"{', '.join(BACKGROUND_METHODS)}"
            )
        degree = _int(self.degree, "degree")
        if not 0 <= degree <= 10:
            raise EnhanceError(f"degree must be an int in [0, 10], got {self.degree!r}")


def _block_stats(arr2d: np.ndarray, cell: int, agg: str) -> np.ndarray:
    """Per-block statistics of a 2-D array on a grid of ``cell``-sized blocks:
    ``agg='median'`` -> block medians, ``agg='mean'`` -> block means. Blocks
    with no finite values get NaN."""
    height, width = arr2d.shape
    gh = -(-height // cell)
    gw = -(-width // cell)
    stats = np.full((gh, gw), np.nan, dtype=np.float64)
    for i in range(gh):
        for j in range(gw):
            block = arr2d[i * cell : (i + 1) * cell, j * cell : (j + 1) * cell]
            vals = _finite_values(block)
            if vals.size:
                stats[i, j] = float(np.median(vals)) if agg == "median" else float(vals.mean())
    return stats


def _estimate_background_2d(arr2d: np.ndarray, params: BackgroundParams) -> np.ndarray:
    """Estimate the background of a 2-D float image as a full-size float64
    array (NaN pixels excluded from the statistics)."""
    height, width = arr2d.shape
    cell = max(BACKGROUND_MIN_CELL, min(height, width) // BACKGROUND_GRID_DIVISOR)
    if params.method == "median":
        stats = _block_stats(arr2d, cell, "median")
        finite_stats = stats[np.isfinite(stats)]
        fill = float(np.median(finite_stats)) if finite_stats.size else 0.0
        stats = np.where(np.isfinite(stats), stats, fill)
        bg = np.repeat(np.repeat(stats, cell, axis=0), cell, axis=1)
        return bg[:height, :width]
    # poly: fit a polynomial surface of degree `degree` to the block means
    degree = params.degree
    means = _block_stats(arr2d, cell, "mean")
    gh, gw = means.shape
    ys, xs = np.mgrid[0:gh, 0:gw]
    valid = np.isfinite(means)
    if valid.sum() < (degree + 1) * (degree + 2) // 2:
        # too few sample points for the requested degree: fall back to the
        # global median (a constant background)
        vals = _finite_values(arr2d)
        return np.full((height, width), float(np.median(vals)) if vals.size else 0.0)
    xn = 2.0 * (xs[valid] * cell + cell / 2.0) / width - 1.0
    yn = 2.0 * (ys[valid] * cell + cell / 2.0) / height - 1.0
    terms = [(i, j) for i in range(degree + 1) for j in range(degree + 1 - i)]
    design = np.stack([xn ** i * yn ** j for i, j in terms], axis=1)
    coeffs, *_ = np.linalg.lstsq(design, means[valid], rcond=None)
    px = 2.0 * (np.arange(width, dtype=np.float64) + 0.5) / width - 1.0
    py = 2.0 * (np.arange(height, dtype=np.float64) + 0.5) / height - 1.0
    xx, yy = np.meshgrid(px, py)
    bg = np.zeros((height, width), dtype=np.float64)
    for coeff, (i, j) in zip(coeffs, terms):
        bg += coeff * xx ** i * yy ** j
    return bg


def apply_background(img: np.ndarray, params: BackgroundParams) -> np.ndarray:
    """Subtract the estimated background from ``img`` (float32, NaN mask
    preserved): ``out = img - background``.

    - ``median`` — the image is split into blocks (about
      ``min(H, W) // 8`` pixels), the per-block median is computed, and the
      grid is nearest-upsampled to a full-size background;
    - ``poly`` — a polynomial surface of degree ``degree`` in normalized
      pixel coordinates is least-squares-fitted to the downsampled block
      means and evaluated on every pixel.

    For RGB the background is estimated per channel. Flattens gradients and
    sky glow; the residual is centered near 0.
    """
    if not isinstance(params, BackgroundParams):
        raise EnhanceError(f"apply_background expects BackgroundParams, got {params!r}")
    arr = _as_image(img)
    if arr.ndim == 2:
        bg = _estimate_background_2d(arr, params)
    else:
        bg = np.stack(
            [_estimate_background_2d(arr[..., c], params) for c in range(3)], axis=-1
        )
    out = arr.astype(np.float64) - bg
    return _restore_nan_mask(out, arr)


# --- registry ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class OpSpec:
    """One registered op: its ``name``, the params dataclass type, and the
    pure apply function ``apply(img, params) -> img``."""

    name: str
    params_type: type
    apply: Callable[[np.ndarray, Any], np.ndarray]


#: The op registry: op name -> :class:`OpSpec` (used by :mod:`.pipeline`).
OPS: dict[str, OpSpec] = {
    "stretch": OpSpec("stretch", StretchParams, apply_stretch),
    "denoise": OpSpec("denoise", DenoiseParams, apply_denoise),
    "sharpen": OpSpec("sharpen", SharpenParams, apply_sharpen),
    "white_balance": OpSpec("white_balance", WhiteBalanceParams, apply_white_balance),
    "background": OpSpec("background", BackgroundParams, apply_background),
}

#: The registered op names, in definition order.
OP_NAMES: tuple[str, ...] = tuple(OPS)
