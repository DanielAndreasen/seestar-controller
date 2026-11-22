"""The pure median-combine math + field-rotation helpers of the stacking
package.

Pure Python — no GTK, no link, no astropy: stdlib + :mod:`numpy` only (the
same boundary as :mod:`seestar_controller.mosaic.core`). The combine is a
plain pixel-wise **median** over the leading axis, which also rejects
cosmic-ray hits and satellite trails for free.

Field rotation (stacking issue 01 — alt-az mounts rotate the field during a
session, so a pixel-wise median of unregistered subs smears stars into
arcs):

- :func:`header_rotation_deg` — the per-frame field rotation (the position
  angle of the image's +y axis / north, in degrees east of north) from a
  header's ``CD`` matrix (or ``PC`` x ``CDELT``), pure card math;
- :func:`total_field_rotation_deg` — the total rotation over a session
  (largest pairwise difference of the per-sub rotations);
- :func:`max_rotation_displacement_px` /
  :func:`rotation_exceeds_threshold` — how far the farthest corner of an
  image moves under that rotation (in pixels), and whether that exceeds
  :data:`DEROTATE_THRESHOLD_PX` (~1 star FWHM at typical ~2 px seeing);
- :func:`derotate_array` — rotate one image about its center by a given
  pixel position angle (scipy, lazily imported) so it aligns with a
  reference rotation; :func:`median_combine` then takes the median of the
  derotated subs (``nan_safe=True`` ignores the NaN-filled corners).

Without any WCS the combine stays a plain pixel-wise median (documented
assumption, see :mod:`.session`).
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import numpy as np

from .errors import StackSubsError

#: Derotation threshold in pixels: the total session field rotation is
#: compensated when it moves the farthest image corner by at least this many
#: pixels — ~1 star FWHM at a typical ~2 px seeing (documented assumption).
DEROTATE_THRESHOLD_PX = 2.0


def median_combine(arrays: Iterable[Any], *, nan_safe: bool = False) -> np.ndarray:
    """Median-combine ``arrays`` (image arrays of EQUAL shape) pixel-wise:
    ``median(arrays, axis=0)`` over a new leading axis.

    The result keeps the input dtype for floating inputs (the median of an
    even number of frames averages the two middle values, then casts back).

    ``nan_safe=True`` takes a NaN-aware median instead (used after
    :func:`derotate_array`, whose corners are NaN-filled): pixels that are
    NaN in EVERY sub stay NaN, pixels NaN in only some subs use the median
    of the rest.

    Raises :class:`TypeError` when ``arrays`` is not an iterable of numpy
    arrays, and :class:`~.errors.StackSubsError` when the sequence is empty
    or the shapes differ.
    """
    if isinstance(arrays, (str, bytes)) or not isinstance(arrays, Iterable):
        raise TypeError(f"arrays must be an iterable of numpy arrays, got {arrays!r}")
    arrs = list(arrays)
    if not arrs:
        raise StackSubsError("median_combine of an empty sequence")
    for arr in arrs:
        if not isinstance(arr, np.ndarray):
            raise TypeError(
                f"arrays must contain only numpy arrays, got {type(arr).__name__}"
            )
    shape = arrs[0].shape
    for arr in arrs[1:]:
        if arr.shape != shape:
            raise StackSubsError(
                f"shape mismatch: {arr.shape} does not match the first array's {shape}"
            )
    stacked = np.stack(arrs)
    if nan_safe and np.issubdtype(stacked.dtype, np.floating) and np.isnan(stacked).any():
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN edge slices
            combined = np.nanmedian(stacked, axis=0)
    else:
        combined = np.median(stacked, axis=0)
    if np.issubdtype(arrs[0].dtype, np.floating) and combined.dtype != arrs[0].dtype:
        combined = combined.astype(arrs[0].dtype)
    return combined


# --- field rotation (pure card math + one lazy scipy rotate) --------------------------


def _card_float(cards: Mapping[str, Any], key: str) -> float | None:
    """One FITS card as a finite float, or None when missing/non-numeric."""
    value = cards.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _card_floats(cards: Mapping[str, Any], keys: Sequence[str]) -> list[float] | None:
    """The named cards as a list of finite floats, or None when any of them
    is missing / non-numeric."""
    values: list[float] = []
    for key in keys:
        value = _card_float(cards, key)
        if value is None:
            return None
        values.append(value)
    return values


def _cd_matrix(cards: Mapping[str, Any]) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """The effective 2x2 ``CD`` matrix of a header as ((cd11, cd12),
    (cd21, cd22)), from the ``CDi_j`` cards or from ``PCi_j`` x ``CDELT``;
    None when neither is complete."""
    cd = _card_floats(cards, ("CD1_1", "CD1_2", "CD2_1", "CD2_2"))
    if cd is not None:
        cd11, cd12, cd21, cd22 = cd
        return (cd11, cd12), (cd21, cd22)
    pc = _card_floats(cards, ("PC1_1", "PC1_2", "PC2_1", "PC2_2"))
    cdelt1 = _card_float(cards, "CDELT1")
    cdelt2 = _card_float(cards, "CDELT2")
    if pc is not None and cdelt1 is not None and cdelt2 is not None:
        pc11, pc12, pc21, pc22 = pc
        return (
            (pc11 * cdelt1, pc12 * cdelt2),
            (pc21 * cdelt1, pc22 * cdelt2),
        )
    return None


def header_rotation_deg(cards: Mapping[str, Any]) -> float | None:
    """The field rotation of a frame from its FITS ``cards``: the position
    angle of the image's +y axis (north) in degrees EAST of north,
    ``atan2(CD1_2, CD2_2)`` — 0.0 for a plain north-up ``CDELT`` header.

    ``cards`` — a mapping of FITS cards (a solved WCS's cards or a FITS
    header dict, see
    :func:`seestar_controller.store.frames.header_to_dict`). Returns None
    when the cards carry no celestial WCS at all (no ``CD``/``PC`` matrix
    and no ``CTYPE`` RA/DEC) — the caller then has no rotation estimate.
    """
    if not isinstance(cards, Mapping):
        raise TypeError(f"cards must be a mapping of FITS cards, got {cards!r}")
    ctype1 = str(cards.get("CTYPE1", "")).upper()
    ctype2 = str(cards.get("CTYPE2", "")).upper()
    has_celestial = "RA" in ctype1 and "DEC" in ctype2
    cd = _cd_matrix(cards)
    if cd is None:
        # no matrix: a CDELT-only celestial header is north-up (rotation 0)
        if has_celestial and _card_float(cards, "CDELT2") is not None:
            return 0.0
        return None
    (cd11, cd12), (cd21, cd22) = cd
    if cd11 == 0.0 and cd22 == 0.0 and cd12 == 0.0 and cd21 == 0.0:
        return None
    return math.degrees(math.atan2(cd12, cd22))


def wrap_180(delta_deg: float) -> float:
    """Wrap an angle difference in degrees to ``[-180, 180)``."""
    return (delta_deg + 180.0) % 360.0 - 180.0


def total_field_rotation_deg(rotations: Sequence[Any]) -> float | None:
    """The total field rotation over a session from the per-sub rotations
    (degrees, in ``[0, 180]``): the largest pairwise difference, unwrapped
    around the first rotation so the 0/360 wrap does not split the sample
    (a 350° vs 10° session is a 20° rotation, not 340°). Differences beyond
    180° fold back (a 200° rotation blurs like 160°).

    Returns None when fewer than 2 rotations are given (nothing to compare)
    or any entry is not a finite number; 0.0 for a constant series.
    """
    if isinstance(rotations, (str, bytes)) or not isinstance(rotations, Sequence):
        raise TypeError(f"rotations must be a sequence of numbers, got {rotations!r}")
    values: list[float] = []
    for rot in rotations:
        if isinstance(rot, bool) or not isinstance(rot, (int, float)):
            return None
        value = float(rot)
        if not math.isfinite(value):
            return None
        values.append(value)
    if len(values) < 2:
        return None
    base = values[0]
    unwrapped = [wrap_180(v - base) for v in values]
    span = max(unwrapped) - min(unwrapped)
    if span > 180.0:
        span = 360.0 - span
    return span


def max_rotation_displacement_px(rotation_deg: float, shape: Sequence[int]) -> float:
    """How far (in pixels) the farthest corner of an image of the given
    ``shape`` (``(ny, nx)`` — extra axes are ignored) moves under a rotation
    of ``rotation_deg`` about the image center: ``corner_radius * rad``.

    Raises :class:`TypeError` for a non-numeric ``rotation_deg`` and
    :class:`~.errors.StackSubsError` for a shape without at least 2 axes.
    """
    if isinstance(rotation_deg, bool) or not isinstance(rotation_deg, (int, float)):
        raise TypeError(f"rotation_deg must be a number, got {rotation_deg!r}")
    if (
        isinstance(shape, (str, bytes))
        or not isinstance(shape, Sequence)
        or len(shape) < 2
    ):
        raise StackSubsError(f"shape must be a (ny, nx, ...) sequence, got {shape!r}")
    ny, nx = int(shape[0]), int(shape[1])
    if ny < 1 or nx < 1:
        raise StackSubsError(f"shape axes must be positive, got {shape!r}")
    corner_radius = math.hypot(ny, nx) / 2.0
    return corner_radius * math.radians(abs(float(rotation_deg)))


def rotation_exceeds_threshold(
    rotation_deg: float,
    shape: Sequence[int],
    *,
    threshold_px: float = DEROTATE_THRESHOLD_PX,
) -> bool:
    """True when a total field rotation of ``rotation_deg`` moves the
    farthest corner of an image of ``shape`` by at least ``threshold_px``
    pixels (:data:`DEROTATE_THRESHOLD_PX` by default, ~1 star FWHM at a
    typical ~2 px seeing) — i.e. the pixel-wise median would visibly smear.
    """
    return max_rotation_displacement_px(rotation_deg, shape) >= threshold_px


def derotate_array(array: Any, angle_deg: float) -> np.ndarray:
    """Rotate the image ``array`` about its center by ``angle_deg`` in PIXEL
    position angle (positive = from the +y axis toward +x, i.e. eastward on
    the sky for the usual ``CDELT1 < 0`` convention — the scipy
    :func:`scipy.ndimage.rotate` convention, verified against
    :class:`astropy.wcs.WCS`). ``derotate_array(sub, ref_rotation -
    sub_rotation)`` therefore aligns a sub with field rotation
    ``sub_rotation`` to the reference rotation ``ref_rotation``.

    The shape is kept (``reshape=False``); the rotated-in corners are filled
    with NaN (combine them with :func:`median_combine` ``nan_safe=True``).
    2-D ``(ny, nx)`` and channel-last 3-D ``(ny, nx, c)`` arrays both work.
    Linear (order 1) interpolation, so a sharp star stays sharp.

    Raises :class:`TypeError` for a non-array / non-numeric angle and
    :class:`~.errors.StackSubsError` for arrays with fewer than 2 axes.
    """
    if not isinstance(array, np.ndarray):
        raise TypeError(f"array must be a numpy array, got {type(array).__name__}")
    if array.ndim < 2:
        raise StackSubsError(f"array must have at least 2 axes, got {array.shape!r}")
    if isinstance(angle_deg, bool) or not isinstance(angle_deg, (int, float)):
        raise TypeError(f"angle_deg must be a number, got {angle_deg!r}")
    if not math.isfinite(float(angle_deg)):
        raise StackSubsError(f"angle_deg must be finite, got {angle_deg!r}")
    if float(angle_deg) == 0.0:
        return array.copy()
    if not np.issubdtype(array.dtype, np.floating):
        array = array.astype(np.float32)  # NaN corner fill needs a float dtype
    from scipy import ndimage  # lazy: heavy third-party import

    rotated = ndimage.rotate(
        array,
        float(angle_deg),
        axes=(0, 1),
        reshape=False,
        order=1,
        mode="constant",
        cval=np.nan,
    )
    return rotated
