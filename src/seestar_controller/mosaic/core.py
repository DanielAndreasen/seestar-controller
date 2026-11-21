"""Stitch core — align WCS tiles onto a common grid and blend the overlaps.

Pure module of :mod:`seestar_controller.mosaic` (mosaic-stitch ticket 01):
**no ``gi`` import** — stdlib + :mod:`numpy` + :mod:`seestar_controller.store`
only at module level; the heavy :mod:`astropy` imports (and the optional
:mod:`reproject`) stay lazy inside the functions that need them, the same
pattern as :mod:`seestar_controller.frame_quality.metrics`.

- :class:`Tile` — one tile: a name, its image array (2-D or 3-D ``(h, w,
  channels)``) and its FITS header (a mapping of cards, carrying the WCS);
- :func:`tile_wcs` — the tile's celestial :class:`astropy.wcs.WCS` (lazy
  import); a tile without a celestial WCS raises :class:`MosaicError` naming
  the tile;
- :func:`tile_offset_px` — the WCS delta of one tile: where the tile's pixel
  ``(0, 0)`` lands in the reference frame, in (fractional) reference pixels;
- :func:`common_grid` — the common canvas for a set of tiles: the output
  shape, the per-tile integer placement offsets, and the overlap check
  (disconnected tiles raise :class:`MosaicError`);
- :func:`stitch_tiles` — place every tile on the common grid and blend the
  overlaps (``"average"`` or ``"feather"``), returning the mosaic array plus
  the output header cards (the reference WCS shifted onto the canvas).

Alignment strategy (documented choice, see ``.scratch/mosaic-stitch/spec.md``):
when :mod:`reproject` is installed, tiles are reprojected onto the common
grid's WCS (sub-pixel accurate, handles differing pixel scales); otherwise
the fallback shifts each tile by the INTEGER part of its WCS delta (exact
when all tiles share the reference pixel scale, which the fallback checks).

All failures are :class:`MosaicError` (a :class:`ValueError`) with a message
naming the offending tile(s).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from seestar_controller.store.frames import header_to_dict


class MosaicError(ValueError):
    """A mosaic stitch failure — a :class:`ValueError` with a clear message:
    a tile without a celestial WCS, tiles that do not overlap, a differing
    pixel scale in the shift fallback, an unreadable tile, or a bad
    parameter."""


#: Overlap blend mode: plain average of the overlapping tiles.
BLEND_AVERAGE = "average"
#: Overlap blend mode: feather (linear ramp from each tile edge over
#: ``feather_px`` pixels) before averaging.
BLEND_FEATHER = "feather"
#: The valid blend modes.
BLENDS = (BLEND_AVERAGE, BLEND_FEATHER)

#: Default feather width in pixels (see :func:`stitch_tiles`).
DEFAULT_FEATHER_PX = 16.0

#: Relative pixel-scale mismatch tolerated by the integer-shift fallback
#: (tiles of one mosaic session share the scope's pixel scale; a larger
#: mismatch needs :mod:`reproject`).
SCALE_TOLERANCE = 1e-3


@dataclass(frozen=True)
class Tile:
    """One mosaic tile: ``name`` (for error messages), ``data`` (a 2-D
    ``(h, w)`` or 3-D ``(h, w, channels)`` numeric array) and ``header`` (a
    mapping of FITS cards carrying the tile's celestial WCS)."""

    name: str
    data: np.ndarray
    header: Mapping[str, Any]


@dataclass(frozen=True)
class Grid:
    """The common grid of a set of tiles (see :func:`common_grid`).

    - ``shape`` — the canvas shape ``(height, width)``;
    - ``offsets`` — per-tile INTEGER placement ``(x0, y0)`` in canvas pixels
      (the shift fallback);
    - ``float_offsets`` — per-tile fractional ``(ox, oy)`` of the tile's
      pixel ``(0, 0)`` in reference pixels (the WCS delta);
    - ``min_x`` / ``min_y`` — the canvas origin in reference pixels (the
      output WCS is the reference WCS shifted by ``(-min_x, -min_y)``);
    - ``ref_index`` — the index of the reference tile (its WCS defines the
      canvas).
    """

    shape: tuple[int, int]
    offsets: tuple[tuple[int, int], ...]
    float_offsets: tuple[tuple[float, float], ...]
    min_x: float
    min_y: float
    ref_index: int


def _check_tile(tile: Tile, index: int) -> None:
    """Validate one tile's fields; raise :class:`MosaicError` naming it."""
    if not isinstance(tile, Tile):
        raise MosaicError(f"tiles[{index}] must be a Tile, got {tile!r}")
    if not isinstance(tile.name, str) or not tile.name:
        raise MosaicError(f"tiles[{index}].name must be a non-empty string")
    data = np.asarray(tile.data)
    if data.ndim not in (2, 3):
        raise MosaicError(
            f"tile {tile.name!r} must be a 2-D (h, w) or 3-D (h, w, channels) "
            f"array, got shape {data.shape}"
        )
    if data.shape[0] < 1 or data.shape[1] < 1:
        raise MosaicError(f"tile {tile.name!r} is empty (shape {data.shape})")
    if not np.issubdtype(data.dtype, np.number):
        raise MosaicError(f"tile {tile.name!r} has a non-numeric dtype {data.dtype}")
    if not isinstance(tile.header, Mapping):
        raise MosaicError(f"tile {tile.name!r} header must be a mapping of FITS cards")


def tile_wcs(tile: Tile) -> Any:
    """The celestial :class:`astropy.wcs.WCS` of ``tile`` (lazy astropy
    import).

    Raises :class:`MosaicError` naming the tile when its header carries no
    usable celestial WCS (no ``CTYPE``/``CRVAL`` or an unparseable header).
    """
    _check_tile(tile, 0)
    from astropy.wcs import WCS

    try:
        # naxis=2: a 3-D (h, w, channels) tile's header has NAXIS=3, but the
        # WCS is the 2-D celestial one of the first two axes
        wcs = WCS(tile.header, naxis=2)
    except Exception as exc:
        raise MosaicError(
            f"tile {tile.name!r} has no usable WCS in its FITS header: {exc}"
        ) from exc
    if not getattr(wcs, "has_celestial", False):
        raise MosaicError(
            f"tile {tile.name!r} has no celestial WCS (CTYPE/CRVAL missing) — "
            "cannot align it; plate-solve the tile first"
        )
    return wcs


def _pixel_scale_deg(wcs: Any) -> float:
    """The mean pixel scale of a celestial WCS in degrees/pixel."""
    scales = wcs.proj_plane_pixel_scales()  # Quantity deg/pixel per axis
    values = [float(getattr(s, "value", s)) for s in scales]
    return sum(values) / len(values)


def tile_offset_px(ref_wcs: Any, tile: Tile) -> tuple[float, float]:
    """The WCS delta of ``tile`` against the reference WCS ``ref_wcs``: the
    ``(ox, oy)`` position of the tile's pixel ``(0, 0)`` in reference pixels
    (fractional). With a shared pixel scale, tile pixel ``(x, y)`` lands at
    reference pixel ``(ox + x, oy + y)``."""
    tile_wcs_ = tile_wcs(tile)
    sky = tile_wcs_.pixel_to_world(0, 0)  # SkyCoord of the tile's corner
    ox, oy = ref_wcs.world_to_pixel(sky)
    return float(ox), float(oy)


def _reproject_available() -> bool:
    """Whether the optional :mod:`reproject` package is importable (lazy)."""
    try:
        import reproject  # noqa: F401  # ty: ignore[import-not-found]
    except Exception:  # noqa: BLE001 — any import failure means "not installed"
        return False
    return True


def _resolve_use_reproject(use_reproject: bool | None) -> bool:
    """Resolve the ``use_reproject`` parameter: None = auto (use reproject
    when installed), True = require it (MosaicError when missing), False =
    never (integer-shift fallback)."""
    if use_reproject is None:
        return _reproject_available()
    if not isinstance(use_reproject, bool):
        raise MosaicError(f"use_reproject must be a bool or None, got {use_reproject!r}")
    if use_reproject and not _reproject_available():
        raise MosaicError(
            "use_reproject=True but the optional 'reproject' package is not "
            "installed — the integer-shift fallback is used instead by default"
        )
    return use_reproject


def _check_overlap(names: Sequence[str], rects: Sequence[tuple[float, float, float, float]]) -> None:
    """Require the tiles' sky rectangles to form ONE connected overlap graph;
    raise :class:`MosaicError` naming the disconnected tiles otherwise."""
    n = len(rects)
    if n < 2:
        return
    # pairwise intersection area > 0 -> an edge
    adjacent: list[list[int]] = [[] for _ in range(n)]
    for i in range(n):
        x0, y0, x1, y1 = rects[i]
        for j in range(i + 1, n):
            u0, v0, u1, v1 = rects[j]
            ix = min(x1, u1) - max(x0, u0)
            iy = min(y1, v1) - max(y0, v0)
            if ix > 0 and iy > 0:
                adjacent[i].append(j)
                adjacent[j].append(i)
    # BFS from tile 0
    seen = {0}
    stack = [0]
    while stack:
        i = stack.pop()
        for j in adjacent[i]:
            if j not in seen:
                seen.add(j)
                stack.append(j)
    if len(seen) < n:
        lost = [names[i] for i in range(n) if i not in seen]
        raise MosaicError(
            "tiles do not overlap — no shared sky area between "
            f"{', '.join(repr(nm) for nm in lost)} and the other tiles; "
            "cannot stitch a disconnected mosaic"
        )


def common_grid(tiles: Sequence[Tile], *, use_reproject: bool | None = None) -> Grid:
    """Compute the common grid for ``tiles`` (see :class:`Grid`).

    The FIRST tile is the reference: its WCS defines the canvas, and every
    other tile's WCS delta (see :func:`tile_offset_px`) gives its placement.
    The canvas spans all tiles (origin = floor of the minimum offset).

    Raises :class:`MosaicError` when a tile has no celestial WCS, when the
    tiles do not overlap (disconnected), when the tiles have mixed shapes,
    or — in the integer-shift fallback — when a tile's pixel scale differs
    from the reference's by more than :data:`SCALE_TOLERANCE`.
    """
    if not isinstance(tiles, Sequence) or isinstance(tiles, (str, bytes)):
        raise MosaicError(f"tiles must be a sequence of Tile, got {tiles!r}")
    tiles = list(tiles)
    if not tiles:
        raise MosaicError("no tiles to stitch (empty sequence)")
    for i, tile in enumerate(tiles):
        _check_tile(tile, i)
    ref = tiles[0]
    ref_data = np.asarray(ref.data)
    for i, tile in enumerate(tiles[1:], start=1):
        other = np.asarray(tile.data)
        if other.ndim != ref_data.ndim or (
            other.ndim == 3 and other.shape[2] != ref_data.shape[2]
        ):
            raise MosaicError(
                f"tile {tile.name!r} has shape {other.shape} but the reference "
                f"tile {ref.name!r} has shape {ref_data.shape} — all tiles must "
                "share the same (h, w[, channels]) layout"
            )

    use_reproj = _resolve_use_reproject(use_reproject)
    ref_wcs = tile_wcs(ref)
    ref_scale = _pixel_scale_deg(ref_wcs)

    float_offsets: list[tuple[float, float]] = []
    for tile in tiles:
        if not use_reproj:
            scale = _pixel_scale_deg(tile_wcs(tile))
            if abs(scale - ref_scale) > SCALE_TOLERANCE * ref_scale:
                raise MosaicError(
                    f"tile {tile.name!r} has pixel scale {scale:.6g} deg/px but "
                    f"the reference tile {ref.name!r} has {ref_scale:.6g} deg/px — "
                    "the integer-shift fallback needs a common pixel scale; "
                    "install 'reproject' for full reprojection"
                )
        float_offsets.append(tile_offset_px(ref_wcs, tile))

    # Integer placement: round each WCS delta to the nearest pixel (the
    # deltas are sub-pixel noisy, ~0.03 px for CAR/TAN at these scales, so
    # rounding is exact for integer-offset tiles), then derive the canvas
    # from the INTEGER rects — floor/ceil of the raw floats would amplify
    # the sub-pixel noise into extra canvas rows/columns.
    int_offsets = [
        (math.floor(ox + 0.5), math.floor(oy + 0.5))
        for ox, oy in float_offsets
    ]
    rects = [
        (x0, y0, x0 + np.asarray(tile.data).shape[1], y0 + np.asarray(tile.data).shape[0])
        for tile, (x0, y0) in zip(tiles, int_offsets, strict=True)
    ]
    _check_overlap([t.name for t in tiles], rects)

    min_x = min(r[0] for r in rects)
    min_y = min(r[1] for r in rects)
    max_x = max(r[2] for r in rects)
    max_y = max(r[3] for r in rects)
    shape = (int(max_y - min_y), int(max_x - min_x))
    offsets = tuple((x0 - min_x, y0 - min_y) for x0, y0 in int_offsets)
    return Grid(
        shape=shape,
        offsets=offsets,
        float_offsets=tuple(float_offsets),
        min_x=float(min_x),
        min_y=float(min_y),
        ref_index=0,
    )


def _feather_weight(shape2d: tuple[int, int], feather_px: float) -> np.ndarray:
    """The feather ramp of one tile: a linear ramp from each tile edge over
    ``feather_px`` pixels, clamped to 1 inside. The outermost pixel keeps a
    ``1/feather_px`` weight (``dist + 1``) so the mosaic border stays covered
    (a zero edge weight would leave the outermost row/column uncovered)."""
    h, w = shape2d
    fx = np.minimum(np.arange(w, dtype=np.float64), w - 1 - np.arange(w, dtype=np.float64))
    fy = np.minimum(np.arange(h, dtype=np.float64), h - 1 - np.arange(h, dtype=np.float64))
    wx = np.minimum((fx + 1.0) / feather_px, 1.0)
    wy = np.minimum((fy + 1.0) / feather_px, 1.0)
    return wy[:, None] * wx[None, :]


def _out_wcs(ref_wcs: Any, grid: Grid) -> Any:
    """The canvas WCS: the reference WCS shifted so canvas pixel ``(0, 0)``
    is reference pixel ``(min_x, min_y)`` (CRPIX is 1-based)."""
    out = ref_wcs.deepcopy()
    out.wcs.crpix = [
        float(ref_wcs.wcs.crpix[0]) - grid.min_x,
        float(ref_wcs.wcs.crpix[1]) - grid.min_y,
    ]
    return out


def _place_shift(
    data: np.ndarray, canvas2d: tuple[int, int], offset: tuple[int, int], weight2d: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Place one tile at its integer ``offset`` on the canvas (shift
    fallback); returns the full-canvas data (NaN outside) and weight."""
    h, w = canvas2d
    x0, y0 = offset
    th, tw = data.shape[:2]
    out = np.full((h, w) + data.shape[2:], np.nan, dtype=np.float64)
    wout = np.zeros((h, w), dtype=np.float64)
    out[y0 : y0 + th, x0 : x0 + tw] = data
    wout[y0 : y0 + th, x0 : x0 + tw] = weight2d
    return out, wout


def _place_reproject(
    tile: Tile, out_wcs: Any, canvas2d: tuple[int, int], weight2d: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Reproject one tile onto the canvas WCS (optional :mod:`reproject`);
    returns the full-canvas data (NaN outside the footprint) and weight."""
    from reproject import reproject_interp  # ty: ignore[import-not-found]

    h, w = canvas2d
    data = np.asarray(tile.data, dtype=np.float64)
    tile_wcs_ = tile_wcs(tile)
    if data.ndim == 2:
        arr, footprint = reproject_interp((data, tile_wcs_), out_wcs, shape_out=(h, w))
        return np.asarray(arr, dtype=np.float64), np.asarray(footprint, dtype=np.float64) * weight2d
    channels = []
    weights = []
    for c in range(data.shape[2]):
        arr, footprint = reproject_interp((data[:, :, c], tile_wcs_), out_wcs, shape_out=(h, w))
        channels.append(np.asarray(arr, dtype=np.float64))
        weights.append(np.asarray(footprint, dtype=np.float64))
    arr = np.stack(channels, axis=-1)
    footprint = np.min(np.stack(weights, axis=0), axis=0)  # valid where every channel is
    return arr, footprint * weight2d


def stitch_tiles(
    tiles: Sequence[Tile],
    *,
    blend: str = BLEND_AVERAGE,
    feather_px: float = DEFAULT_FEATHER_PX,
    use_reproject: bool | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Stitch ``tiles`` onto their common grid and blend the overlaps.

    - ``blend`` — :data:`BLEND_AVERAGE` (default) or :data:`BLEND_FEATHER`
      (linear ramp over ``feather_px`` pixels from each tile edge);
    - ``feather_px`` — the feather width in pixels (finite, > 0);
    - ``use_reproject`` — None (default) = reproject when the optional
      :mod:`reproject` package is installed, else the integer-shift
      fallback; True = require reproject; False = always the fallback.

    Returns ``(mosaic, out_cards)``: the mosaic array (float64, same ndim as
    the tiles, NaN where no tile covers) and the output header cards — the
    reference tile's header with ``CRPIX1``/``CRPIX2`` shifted onto the
    canvas (the canvas WCS). Raises :class:`MosaicError` on any failure (see
    :func:`common_grid`).
    """
    if blend not in BLENDS:
        raise MosaicError(f"blend must be one of {BLENDS}, got {blend!r}")
    if isinstance(feather_px, bool) or not isinstance(feather_px, (int, float)):
        raise MosaicError(f"feather_px must be a number, got {feather_px!r}")
    feather_px = float(feather_px)
    if not math.isfinite(feather_px) or feather_px <= 0:
        raise MosaicError(f"feather_px must be finite and > 0, got {feather_px!r}")

    tiles = list(tiles)
    grid = common_grid(tiles, use_reproject=use_reproject)
    use_reproj = _resolve_use_reproject(use_reproject)
    ref = tiles[grid.ref_index]
    ref_wcs = tile_wcs(ref)

    canvas2d = grid.shape
    ndim = np.asarray(ref.data).ndim
    canvas_shape = canvas2d if ndim == 2 else canvas2d + (np.asarray(ref.data).shape[2],)

    acc = np.zeros(canvas_shape, dtype=np.float64)
    wsum = np.zeros(canvas2d, dtype=np.float64)
    for tile, offset in zip(tiles, grid.offsets, strict=True):
        data = np.asarray(tile.data, dtype=np.float64)
        th, tw = data.shape[:2]
        if blend == BLEND_FEATHER:
            ramp = _feather_weight((th, tw), feather_px)
        else:
            ramp = np.ones((th, tw), dtype=np.float64)
        if use_reproj:
            placed, weight = _place_reproject(tile, _out_wcs(ref_wcs, grid), canvas2d, ramp)
        else:
            placed, weight = _place_shift(data, canvas2d, offset, ramp)
        # NaN outside a tile's footprint must not poison the accumulator
        # (NaN * 0 == NaN) — zero it, the weight already masks it out
        placed = np.where(np.isnan(placed), 0.0, placed)
        w = weight if ndim == 2 else weight[..., None]
        acc += placed * w
        wsum += weight

    mosaic = np.full(canvas_shape, np.nan, dtype=np.float64)
    covered = wsum > 0
    if ndim == 2:
        mosaic[covered] = acc[covered] / wsum[covered]
    else:
        mosaic[covered] = acc[covered] / wsum[covered, None]

    cards = header_to_dict(ref.header)
    cards["CRPIX1"] = float(cards.get("CRPIX1", 1.0)) - grid.min_x
    cards["CRPIX2"] = float(cards.get("CRPIX2", 1.0)) - grid.min_y
    return mosaic, cards
