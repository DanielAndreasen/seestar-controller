"""Source detection on a session STACK + WCS from the stack FITS header.

Pure-Python core of the gaia-crossmatch feature (module boundary per
``.scratch/gaia-crossmatch/spec.md``): no GTK imports, no
:mod:`seestar_controller.link` imports — a subprocess test enforces the
boundary. Third-party imports (:mod:`numpy`, :mod:`astropy.wcs`,
:mod:`astropy.stats`, :mod:`photutils.detection`, :mod:`scipy.ndimage`) are
lazy inside the functions that need them, the same pattern as
:mod:`seestar_controller.photometry.picking`.

What lives here:

- :class:`DetectedSource` — one detected source: 0-based pixel ``(x, y)``
  (``x`` = column, ``y`` = row, the same convention as
  :mod:`seestar_controller.photometry.picking`), its J2000 sky position
  ``(ra_deg, dec_deg)`` from the stack WCS, and the DAOStarFinder ``peak``
  / ``flux``;
- :func:`detect_sources` — :class:`photutils.detection.DAOStarFinder` over
  the stack with the FWHM estimated from the image (:func:`estimate_fwhm`)
  unless given, and a configurable detection threshold in units of the
  sigma-clipped background RMS (default :data:`DEFAULT_THRESHOLD_SIGMA`);
- :func:`estimate_fwhm` — the FWHM estimate: second moments of the bright
  connected components above the detection threshold, median over the
  components, clamped to a sane pixel range;
- :func:`pixel_to_sky` / :func:`field_center_and_radius` — the FITS-WCS
  helpers: pixel -> J2000 (0-based pixels, the FITS WCS is 1-based
  internally), and the field center + cone radius (half the sky diagonal of
  the image corners + ``margin_deg``) used for the Gaia cone search.

**Missing WCS -> :class:`~.errors.WcsMissingError`.** ``astropy.wcs.WCS({})``
does NOT raise (it builds an identity WCS with blank ``CTYPE``), so the
helpers check explicitly for celestial ``CTYPE`` cards — the same check as
:func:`seestar_controller.photometry.picking._wcs_from_header`. The error
message tells the user to **plate-solve the stack first**, because the
cross-match needs sky positions.

Non-finite pixels (NaN/inf — stacks can carry them) are replaced by the
sigma-clipped median before detection, so DAOStarFinder never sees a NaN.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from seestar_controller.catalog.coords import J2000Coord, angular_distance
from seestar_controller.store.frames import header_to_dict

from .errors import DetectionError, WcsMissingError

if TYPE_CHECKING:
    from astropy.io.fits import Header

    #: A FITS header: a mapping of cards or an astropy ``Header`` — both are
    #: accepted by :func:`seestar_controller.store.frames.header_to_dict`;
    #: anything else raises :class:`WcsMissingError` at runtime.
    HeaderLike = Mapping[str, Any] | Header
else:
    HeaderLike = Any

#: Default detection threshold, in units of the sigma-clipped background RMS:
#: a source peak must exceed ``median + threshold * std``.
DEFAULT_THRESHOLD_SIGMA = 5.0

#: Fallback FWHM in pixels when the image has no measurable bright blob.
DEFAULT_FWHM_PX = 4.0

#: Clamp range for the estimated FWHM, in pixels.
MIN_FWHM_PX = 1.0
MAX_FWHM_PX = 25.0

#: Minimum number of pixels of a bright blob for its second moment to count.
_MIN_BLOB_PIXELS = 5

#: Default cone-search margin added to the field's sky diagonal, in degrees.
DEFAULT_MARGIN_DEG = 0.05

#: FWHM of a Gaussian from its sigma: ``2 * sqrt(2 * ln 2)``.
_FWHM_PER_SIGMA = 2.0 * math.sqrt(2.0 * math.log(2.0))


@dataclass(frozen=True)
class DetectedSource:
    """One source detected on the stack.

    - ``x`` / ``y`` — 0-based pixel position (``x`` = column, ``y`` = row);
    - ``ra_deg`` / ``dec_deg`` — the J2000 sky position from the stack WCS;
    - ``peak`` — the DAOStarFinder peak data value;
    - ``flux`` — the DAOStarFinder (kernel-convolved) flux.
    """

    x: float
    y: float
    ra_deg: float
    dec_deg: float
    peak: float
    flux: float

    @property
    def xy(self) -> tuple[float, float]:
        """``(x, y)`` — the 0-based pixel position."""
        return self.x, self.y

    @property
    def coord(self) -> J2000Coord:
        """The J2000 sky position as the shared :class:`J2000Coord` type."""
        return J2000Coord(ra_deg=self.ra_deg, dec_deg=self.dec_deg)


# --- input validation -----------------------------------------------------------


def _as_image(stack_data: Any) -> np.ndarray:
    """Coerce ``stack_data`` to a 2-D float64 numpy array.

    Raises :class:`DetectionError` when it is not a 2-D array-like of finite
    size (a stack is always a 2-D grayscale image).
    """
    try:
        data = np.asarray(stack_data, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise DetectionError(f"stack_data must be a 2-D numeric array: {exc}") from exc
    if data.ndim != 2 or data.size == 0:
        raise DetectionError(
            f"stack_data must be a non-empty 2-D array, got shape {data.shape}"
        )
    return data


def _finite_float(value: Any, what: str) -> float:
    """Coerce ``value`` to a finite float (bools rejected)."""
    if isinstance(value, bool):
        raise DetectionError(f"{what} must be a number, got {value!r}")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise DetectionError(f"{what} must be a number, got {value!r}") from None
    if not math.isfinite(result):
        raise DetectionError(f"{what} must be finite, got {value!r}")
    return result


def _validate_threshold(threshold: Any) -> float:
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(threshold)
        or threshold <= 0
    ):
        raise DetectionError(
            f"threshold must be a positive number of background sigmas, got {threshold!r}"
        )
    return float(threshold)


def _validate_fwhm(fwhm: Any) -> float:
    if (
        isinstance(fwhm, bool)
        or not isinstance(fwhm, (int, float))
        or not math.isfinite(fwhm)
        or not 0.5 <= fwhm <= 50.0
    ):
        raise DetectionError(
            f"fwhm must be a number of pixels in [0.5, 50], got {fwhm!r}"
        )
    return float(fwhm)


# --- WCS helpers ------------------------------------------------------------------


def _wcs_from_header(header: Any) -> Any:
    """Build an :class:`astropy.wcs.WCS` from a FITS ``header`` mapping.

    The mapping is first converted with
    :func:`seestar_controller.store.frames.header_to_dict` (sanitized plain
    dict — real Seestar FITS carry ``COMMENT`` cards whose joined value
    breaks ``WCS(dict)``), the same pattern as
    :mod:`seestar_controller.photometry.picking`.

    Raises :class:`WcsMissingError` when the header is not a mapping, the
    WCS cannot be built, or the header carries no celestial WCS (blank
    ``CTYPE`` — ``WCS({})`` alone does not raise, it builds an identity
    WCS).
    """
    if not isinstance(header, Mapping):
        raise WcsMissingError(
            f"header must be a mapping of FITS cards, got {header!r} — "
            "plate-solve the stack first so its header carries a WCS"
        )
    from astropy.wcs import WCS

    try:
        wcs = WCS(header_to_dict(header))
    except Exception as exc:
        raise DetectionError(f"cannot build a WCS from the header: {exc}") from exc
    if wcs.naxis < 2 or not any(str(ctype).strip() for ctype in wcs.wcs.ctype):
        raise WcsMissingError(
            "the stack FITS header has no WCS (no celestial CTYPE cards) — "
            "plate-solve the stack first, the Gaia cross-match needs sky "
            "positions for the detected sources"
        )
    return wcs


def pixel_to_sky(header: HeaderLike | None, x: float, y: float) -> J2000Coord:
    """Convert a 0-based image pixel ``(x, y)`` (``x`` = column, ``y`` = row)
    to a J2000 sky position via the FITS WCS of ``header``.

    The FITS WCS is 1-based, so internally ``all_pix2world(x + 1, y + 1, 1)``
    is called. Raises :class:`WcsMissingError` when the header has no
    celestial WCS ("plate-solve first"), :class:`DetectionError` when
    ``x``/``y`` are not finite numbers or the WCS yields non-finite
    coordinates.
    """
    px = _finite_float(x, "x")
    py = _finite_float(y, "y")
    wcs = _wcs_from_header(header)
    try:
        ra, dec = wcs.all_pix2world([px + 1.0], [py + 1.0], 1)  # 1-based origin
    except Exception as exc:
        raise DetectionError(f"WCS pixel->sky failed for pixel ({px}, {py}): {exc}") from exc
    ra_deg = float(ra[0]) % 360.0
    dec_deg = float(dec[0])
    if not (math.isfinite(ra_deg) and math.isfinite(dec_deg)):
        raise DetectionError(
            f"WCS pixel->sky gave non-finite coordinates for pixel ({px}, {py}): "
            f"ra={ra_deg} dec={dec_deg}"
        )
    return J2000Coord(ra_deg=ra_deg, dec_deg=dec_deg)


def field_center_and_radius(
    header: HeaderLike | None,
    shape: tuple[int, int],
    *,
    margin_deg: float = DEFAULT_MARGIN_DEG,
) -> tuple[J2000Coord, float]:
    """The Gaia cone-search center and radius for an image of ``shape``.

    ``shape`` is the data shape ``(ny, nx)``. The center is the sky position
    of the image center pixel; the radius is the largest great-circle
    distance from the center to the four image corners (half the field
    diagonal on the sky) plus ``margin_deg`` — so the cone covers the whole
    field with a margin. Returns ``(center, radius_deg)``.

    Raises :class:`WcsMissingError` when the header has no celestial WCS.
    """
    if (
        not isinstance(shape, (tuple, list))
        or len(shape) != 2
        or isinstance(shape[0], bool)
        or not isinstance(shape[0], (int, np.integer))
        or isinstance(shape[1], bool)
        or not isinstance(shape[1], (int, np.integer))
        or shape[0] < 1
        or shape[1] < 1
    ):
        raise DetectionError(f"shape must be a (ny, nx) pair of positive ints, got {shape!r}")
    if (
        isinstance(margin_deg, bool)
        or not isinstance(margin_deg, (int, float))
        or not math.isfinite(margin_deg)
        or margin_deg < 0
    ):
        raise DetectionError(f"margin_deg must be a non-negative number, got {margin_deg!r}")
    ny, nx = int(shape[0]), int(shape[1])
    center = pixel_to_sky(header, (nx - 1) / 2.0, (ny - 1) / 2.0)
    corners = ((0.0, 0.0), (nx - 1.0, 0.0), (0.0, ny - 1.0), (nx - 1.0, ny - 1.0))
    radius_deg = max(angular_distance(center, pixel_to_sky(header, cx, cy)) for cx, cy in corners)
    return center, radius_deg + float(margin_deg)


# --- background stats + FWHM estimate ----------------------------------------------


def _background_stats(data: np.ndarray) -> tuple[float, float]:
    """Sigma-clipped ``(median, std)`` of the finite pixels of ``data``.

    Raises :class:`DetectionError` when the image has no finite pixel.
    """
    from astropy.stats import sigma_clipped_stats

    finite = data[np.isfinite(data)]
    if finite.size == 0:
        raise DetectionError("stack_data has no finite pixel — cannot detect sources")
    _, median, std = sigma_clipped_stats(finite, sigma=3.0)
    median = float(median)
    std = float(std)
    if not math.isfinite(std) or std <= 0.0:
        # A perfectly flat image (std == 0): any positive threshold works.
        std = max(std, 1e-12)
    return median, std


def estimate_fwhm(
    stack_data: Any, *, threshold_sigma: float = DEFAULT_THRESHOLD_SIGMA
) -> float:
    """Estimate the FWHM of the stars in ``stack_data``, in pixels.

    The bright connected components above ``threshold_sigma`` background
    sigmas are labeled (:mod:`scipy.ndimage`); for each component with at
    least :data:`_MIN_BLOB_PIXELS` pixels the intensity-weighted second
    moments of the background-subtracted values give a Gaussian sigma, and
    ``fwhm = 2*sqrt(2*ln2) * sigma``. The result is the median over the
    components, clamped to ``[MIN_FWHM_PX, MAX_FWHM_PX]``; with no measurable
    blob it falls back to :data:`DEFAULT_FWHM_PX`.
    """
    from scipy import ndimage

    data = _as_image(stack_data)
    checked = _validate_threshold(threshold_sigma)
    median, std = _background_stats(data)
    subtracted = np.where(np.isfinite(data), data - median, 0.0)
    mask = subtracted > checked * std
    labels, count = ndimage.label(mask)
    fwhms: list[float] = []
    for index in range(1, count + 1):
        ys, xs = np.where(labels == index)
        if ys.size < _MIN_BLOB_PIXELS:
            continue
        weights = subtracted[ys, xs]
        total = float(weights.sum())
        if total <= 0.0:
            continue
        cx = float((weights * xs).sum() / total)
        cy = float((weights * ys).sum() / total)
        var_x = float((weights * (xs - cx) ** 2).sum() / total)
        var_y = float((weights * (ys - cy) ** 2).sum() / total)
        sigma = math.sqrt(max((var_x + var_y) / 2.0, 1e-6))
        fwhms.append(_FWHM_PER_SIGMA * sigma)
    if not fwhms:
        return DEFAULT_FWHM_PX
    return float(min(MAX_FWHM_PX, max(MIN_FWHM_PX, float(np.median(fwhms)))))


# --- the detection ------------------------------------------------------------------


def detect_sources(
    stack_data: Any,
    header: HeaderLike | None,
    *,
    threshold: float = DEFAULT_THRESHOLD_SIGMA,
    fwhm: float | None = None,
) -> list[DetectedSource]:
    """Detect all sources on a session STACK and give each a sky position.

    - ``stack_data`` — the stack image, a 2-D array-like (NaN/inf pixels are
      replaced by the sigma-clipped median before detection);
    - ``header`` — the stack FITS header (a mapping of cards); its celestial
      WCS converts every detection to J2000 ``(ra_deg, dec_deg)``;
    - ``threshold`` — the detection threshold in units of the sigma-clipped
      background RMS (default :data:`DEFAULT_THRESHOLD_SIGMA` = 5): a source
      peak must exceed ``median + threshold * std``;
    - ``fwhm`` — the DAOStarFinder FWHM in pixels; ``None`` (default)
      estimates it from the image with :func:`estimate_fwhm`.

    Returns the detections sorted by flux, brightest first. An image with no
    source above the threshold returns ``[]`` — a normal outcome.

    Raises:
        WcsMissingError: the header has no celestial WCS — "plate-solve the
            stack first" (raised before any detection runs);
        DetectionError: bad image / bad ``threshold`` / bad ``fwhm`` / a WCS
            that yields non-finite sky positions.
    """
    from photutils.detection import DAOStarFinder

    data = _as_image(stack_data)
    checked_threshold = _validate_threshold(threshold)
    wcs = _wcs_from_header(header)  # WcsMissingError first: plate-solve first
    checked_fwhm = (
        estimate_fwhm(data, threshold_sigma=checked_threshold)
        if fwhm is None
        else _validate_fwhm(fwhm)
    )
    median, std = _background_stats(data)
    clean = np.where(np.isfinite(data), data, median)
    finder = DAOStarFinder(threshold=median + checked_threshold * std, fwhm=checked_fwhm)
    table = finder(clean)
    if table is None or len(table) == 0:
        return []
    sources: list[DetectedSource] = []
    for row in table:
        x = float(row["x_centroid"])
        y = float(row["y_centroid"])
        try:
            ra, dec = wcs.all_pix2world([x + 1.0], [y + 1.0], 1)  # 1-based origin
        except Exception as exc:
            raise DetectionError(f"WCS pixel->sky failed for pixel ({x}, {y}): {exc}") from exc
        ra_deg = float(ra[0]) % 360.0
        dec_deg = float(dec[0])
        if not (math.isfinite(ra_deg) and math.isfinite(dec_deg)):
            raise DetectionError(
                f"WCS pixel->sky gave non-finite coordinates for pixel ({x}, {y}): "
                f"ra={ra_deg} dec={dec_deg}"
            )
        sources.append(
            DetectedSource(
                x=x,
                y=y,
                ra_deg=ra_deg,
                dec_deg=dec_deg,
                peak=float(row["peak"]),
                flux=float(row["flux"]),
            )
        )
    sources.sort(key=lambda s: s.flux, reverse=True)
    return sources
