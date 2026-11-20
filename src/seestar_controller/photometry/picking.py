"""Star picking — the PURE layer of :mod:`seestar_controller.photometry`
(photometry ticket 01).

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) — stdlib +
:mod:`numpy` + :mod:`seestar_controller.store` + :mod:`seestar_controller.catalog`
only. The one heavy third-party import, :mod:`astropy.wcs`, is lazy inside the
WCS helpers — the same pattern as :mod:`seestar_controller.enhance.fits_io`
uses for :mod:`astropy.io.fits`. The widget layer (:mod:`.star_picker`) imports
``gi`` at module top and is never imported by this module or eagerly by the
package ``__init__``.

What lives here:

- :class:`PhotometryError` — the package's error base (a :class:`ValueError`);
  :class:`PickingError` (ticket 01) and :class:`~.suggest.SuggestionError`
  (ticket 02) both subclass it, so widget code can catch one type;
- :func:`pixel_to_sky` / :func:`sky_to_pixel` — the FITS-WCS pixel <-> sky
  conversion (:mod:`astropy.wcs`, lazy), plus the lenient variants
  :func:`try_pixel_to_sky` (None instead of raising) and
  :func:`has_celestial_wcs` (bool) used by the picking/suggest paths;
- :class:`StarPickSpec` — one picked star (role, pixel, sky coord, label);
- :class:`ApertureParams` — the aperture radius + sky annulus (pixels) stored
  alongside the picks;
- :class:`PickSession` — the frozen snapshot (image + picks + aperture) handed
  to the listeners;
- :class:`PickController` — the app-level shared controller: the current image
  (a :class:`~seestar_controller.enhance.fits_io.FitsImage`, duck-typed), the
  picks, :meth:`~PickController.pick_at` (click -> optional snap to the local
  brightness centroid -> sky coord when the frame has a WCS -> pick; always
  works in pixel space, see below), :meth:`~PickController.add_at_sky`
  (the inverse: a sky position — e.g. a catalog suggestion, ticket 02 — ->
  WCS pixel -> pick), remove/set_role/clear, aperture
  settings, save/load into the project via
  :func:`seestar_controller.store.stars.save_star_picks` /
  :func:`~seestar_controller.store.stars.load_star_picks`, and listeners with
  frozen :class:`PickSession` snapshots.

Conventions (documented choices):

- **Pixel convention: 0-based, ``x`` = column, ``y`` = row.** The FITS WCS
  itself is 1-based, so :func:`pixel_to_sky` / :func:`sky_to_pixel` add/subtract
  1 internally — callers always use 0-based image pixels (the same convention
  as the numpy ``data[y, x]`` array and the store's ``StarPick.x``/``y``).
- **Missing WCS -> pixel-only picks (ticket 05).** Real Seestar sub-frames
  carry NO celestial WCS (no ``CTYPE`` cards), and RGB stacks (``NAXIS=3`` +
  SIP distortion keywords) make a plain ``WCS(dict)`` raise. The strict
  helpers :func:`pixel_to_sky` / :func:`sky_to_pixel` still raise
  :class:`PickingError` on such headers (``WCS({})`` alone does NOT raise —
  it builds an identity WCS with blank ``CTYPE`` — so the check is explicit),
  but :meth:`PickController.pick_at` NEVER requires a WCS: picking always
  works in pixel space, and the sky coord is attached only when a usable 2D
  celestial WCS exists (:func:`has_celestial_wcs` / :func:`try_pixel_to_sky`);
  otherwise the pick is pixel-only (``coord=None``, storable — the store's
  ``ra_deg``/``dec_deg`` are nullable). RGB stacks are handled by building the
  WCS from the 2D celestial subset: the color axis is dropped (``naxis=2``)
  and the SIP distortion is kept in 2D when possible, else the SIP keywords
  are dropped (see :func:`_wcs_from_header`).
- **Snap (documented, kept simple):** :meth:`PickController.pick_at` snaps the
  click to the brightness-weighted centroid of the finite pixels inside a
  ``(2*SNAP_WINDOW_PX+1)`` square window around the click, with weights
  ``clip(value - median(window), 0, None)``. A "source" is found when the
  window has finite pixels and its peak exceeds the window median; otherwise
  (empty/flat window) the exact click pixel is used. ``snap=False`` disables
  snapping entirely.
- **One target, one check, replace semantics:** re-picking the target
  replaces the existing target pick; re-picking the check star (check-star
  role, check-star-plot ticket 01) replaces the existing check pick; comps
  accumulate (multiple allowed). :meth:`PickController.set_role` to
  ``"target"`` or ``"check"`` replaces any other pick of that role the same
  way.
- **Aperture is session-level here, per-pick in the store.** The pick session
  carries ONE :class:`ApertureParams` (radius + sky annulus) alongside the
  picks; on :meth:`PickController.save` it is written into every
  :class:`~seestar_controller.store.stars.StarPick` row (the store schema is
  per-pick), and on :meth:`PickController.load` it is taken from the first
  stored pick (the target — the store loads target-first).
- **The controller follows the shared viewer controller:** constructed with
  ``viewer_controller=...`` (or :meth:`PickController.attach_viewer`), it
  listens to the viewer's snapshots and adopts every newly opened stack's
  :class:`~seestar_controller.enhance.fits_io.FitsImage` as its current image
  (the last opened image is kept when the viewer clears).
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from seestar_controller.catalog.coords import J2000Coord
from seestar_controller.store.errors import ProjectError, StarPickError
from seestar_controller.store.frames import header_to_dict
from seestar_controller.store.stars import (
    ROLE_CHECK,
    ROLE_COMP,
    ROLE_TARGET,
    ROLES,
    StarPick,
    load_star_picks,
    save_star_picks,
)

logger = logging.getLogger(__name__)


class PhotometryError(ValueError):
    """Base class of every photometry-layer error — a :class:`ValueError`.

    :class:`PickingError` (ticket 01, star picking) and
    :class:`~seestar_controller.photometry.suggest.SuggestionError` (ticket 02,
    catalog assist) both subclass it, so callers (and the widget layer) can
    catch one type for any photometry failure.
    """


class PickingError(PhotometryError):
    """Any star-picking failure — a :class:`ValueError` with a clear message:
    a missing/invalid WCS, a click outside the image, a bad pick field, a bad
    aperture value, a bad index, no image/project set, or a store failure
    while saving/loading the picks."""


# --- WCS pixel <-> sky ---------------------------------------------------------


def _finite_float(value: Any, what: str) -> float:
    """Coerce ``value`` to a finite float (bools rejected); raise
    :class:`PickingError` otherwise."""
    if isinstance(value, bool):
        raise PickingError(f"{what} must be a number, got {value!r}")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise PickingError(f"{what} must be a number, got {value!r}") from None
    if not math.isfinite(result):
        raise PickingError(f"{what} must be finite, got {value!r}")
    return result


#: FITS SIP distortion keywords (``A_ORDER``, ``B_2_0``, ``AP_1_1``, ...) —
#: dropped when a >2-dimensional header cannot carry them (ticket 05).
_SIP_KEY_RE = re.compile(r"^(?:A|B|AP|BP)_(?:ORDER|DMAX|DMIN|\d+_\d+)$")


def _build_wcs(cards: dict[str, Any]) -> Any:
    """Build an :class:`astropy.wcs.WCS` from sanitized header ``cards``,
    tolerating real Seestar stacks (ticket 05): an RGB stack (``NAXIS=3``)
    with SIP distortion keywords makes a plain ``WCS(cards)`` raise
    ("SIP distortions only work in 2 dimensions"), so the build is retried
    reduced to the first 2 axes (``naxis=2`` — the color axis dropped, SIP
    kept in 2D), and finally with the SIP keywords stripped.

    Raises :class:`PickingError` when no WCS can be built at all.
    """
    from astropy.wcs import WCS

    errors: list[str] = []
    no_sip = {k: v for k, v in cards.items() if not _SIP_KEY_RE.match(k)}
    for attempt, kwargs in ((cards, {}), (cards, {"naxis": 2}), (no_sip, {"naxis": 2})):
        try:
            return WCS(attempt, **kwargs)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 — any astropy/wcslib failure
            errors.append(str(exc).strip().splitlines()[0] if str(exc) else repr(exc))
    raise PickingError(
        "cannot build a WCS from the header: " + "; ".join(errors)
    )


def _wcs_from_header(header: Any, *, direction: str = "pixel to sky") -> Any:
    """Build a usable 2D celestial :class:`astropy.wcs.WCS` from a FITS
    ``header`` mapping.

    The mapping is first converted with
    :func:`seestar_controller.store.frames.header_to_dict` (sanitized plain
    dict): real Seestar FITS carry consecutive ``COMMENT`` cards whose
    joined value contains ``\\n``, and ``WCS(dict)`` re-inserts every card
    into a fresh astropy Header — which raises ``ValueError`` ("FITS header
    values must contain standard printable ASCII characters") on such a
    value. Sanitizing makes the WCS buildable from any real frame header.

    The built WCS is then reduced to its 2D celestial subset
    (``wcs.celestial`` — drops a color/Stokes axis of an RGB stack), so the
    result always has exactly 2 celestial axes.

    Raises :class:`PickingError` when the header is not a mapping, the WCS
    cannot be built, or the header carries no usable 2D celestial WCS (blank
    ``CTYPE`` — ``WCS({})`` alone does not raise, it builds an identity WCS
    whose ``.celestial`` subset has ``naxis == 0``).
    """
    if not isinstance(header, Mapping):
        raise PickingError(
            f"header must be a mapping of FITS cards, got {header!r}"
        )
    wcs = _build_wcs(header_to_dict(header))
    try:
        celestial = wcs.celestial
    except Exception as exc:
        raise PickingError(
            "the FITS header has no WCS (no celestial CTYPE cards) — plate-solve "
            f"the frame first; cannot convert {direction} coordinates: {exc}"
        ) from exc
    if celestial.naxis != 2 or not all(str(ctype).strip() for ctype in celestial.wcs.ctype):
        raise PickingError(
            "the FITS header has no WCS (no celestial CTYPE cards) — plate-solve "
            f"the frame first; cannot convert {direction} coordinates"
        )
    return celestial


def pixel_to_sky(header: Mapping[str, Any] | None, x: float, y: float) -> J2000Coord:
    """Convert a 0-based image pixel ``(x, y)`` (``x`` = column, ``y`` = row)
    to a J2000 sky position via the FITS WCS of ``header``.

    The FITS WCS is 1-based, so internally ``all_pix2world(x + 1, y + 1, 1)``
    is called — callers use 0-based pixels. Raises :class:`PickingError` when
    ``x``/``y`` are not finite numbers, the header has no usable 2D celestial
    WCS (e.g. a raw Seestar sub without ``CTYPE`` cards), or the WCS yields
    non-finite coordinates.
    """
    px = _finite_float(x, "x")
    py = _finite_float(y, "y")
    wcs = _wcs_from_header(header, direction="pixel to sky")
    try:
        ra, dec = wcs.all_pix2world([px + 1.0], [py + 1.0], 1)  # 1-based origin
    except Exception as exc:
        raise PickingError(f"WCS pixel->sky failed for pixel ({px}, {py}): {exc}") from exc
    ra_deg = float(ra[0])
    dec_deg = float(dec[0])
    if not (math.isfinite(ra_deg) and math.isfinite(dec_deg)):
        raise PickingError(
            f"WCS pixel->sky gave non-finite coordinates for pixel ({px}, {py}): "
            f"ra={ra_deg} dec={dec_deg}"
        )
    return J2000Coord(ra_deg=ra_deg, dec_deg=dec_deg)


def _ra_dec_deg(coord: Any) -> tuple[float, float]:
    """Extract ``(ra_deg, dec_deg)`` from a :class:`J2000Coord`, a
    ``(ra_deg, dec_deg)`` pair, or an object with ``ra_deg``/``dec_deg``."""
    if isinstance(coord, J2000Coord):
        return coord.ra_deg, coord.dec_deg
    if isinstance(coord, (tuple, list)) and len(coord) == 2:
        try:
            return float(coord[0]) % 360.0, float(coord[1])
        except (TypeError, ValueError):
            raise PickingError(f"invalid (ra, dec) pair: {coord!r}") from None
    ra = getattr(coord, "ra_deg", None)
    dec = getattr(coord, "dec_deg", None)
    if ra is not None and dec is not None:
        return float(ra) % 360.0, float(dec)
    raise PickingError(
        f"expected a J2000Coord, a (ra_deg, dec_deg) pair, or an object with "
        f"ra_deg/dec_deg, got {coord!r}"
    )


def sky_to_pixel(header: Mapping[str, Any] | None, coord: Any) -> tuple[float, float]:
    """The inverse of :func:`pixel_to_sky`: convert a sky position (a
    :class:`~seestar_controller.catalog.coords.J2000Coord`, a
    ``(ra_deg, dec_deg)`` pair, or a duck-typed object) to a 0-based image
    pixel ``(x, y)`` via the FITS WCS of ``header``.

    Internally ``all_world2pix(ra, dec, 1)`` (1-based) minus 1. Raises
    :class:`PickingError` on a bad coord, a missing WCS (e.g. a raw Seestar
    sub — plate-solve the frame first), or non-finite output.
    """
    ra_deg, dec_deg = _ra_dec_deg(coord)
    wcs = _wcs_from_header(header, direction="sky to pixel")
    try:
        px, py = wcs.all_world2pix([ra_deg], [dec_deg], 1)  # 1-based origin
    except Exception as exc:
        raise PickingError(f"WCS sky->pixel failed for ({ra_deg}, {dec_deg}): {exc}") from exc
    x = float(px[0]) - 1.0
    y = float(py[0]) - 1.0
    if not (math.isfinite(x) and math.isfinite(y)):
        raise PickingError(
            f"WCS sky->pixel gave non-finite pixel for ({ra_deg}, {dec_deg}): "
            f"x={x} y={y}"
        )
    return x, y


def has_celestial_wcs(header: Mapping[str, Any] | None) -> bool:
    """True when a usable 2D celestial WCS can be built from ``header``
    (see :func:`_wcs_from_header` — RGB stacks are reduced to their celestial
    subset, SIP handled). False for raw Seestar subs (no ``CTYPE`` cards) and
    for non-mapping headers — never raises."""
    try:
        _wcs_from_header(header)
    except PickingError:
        return False
    return True


def try_pixel_to_sky(
    header: Mapping[str, Any] | None, x: float, y: float
) -> J2000Coord | None:
    """The lenient variant of :func:`pixel_to_sky`: convert a 0-based pixel
    ``(x, y)`` to a J2000 coord, returning **None** instead of raising when
    the header has no usable 2D celestial WCS (a raw Seestar sub) or the
    conversion fails / yields non-finite coordinates.

    This is what :meth:`PickController.pick_at` uses: picking always works in
    pixel space, the sky coord is attached only when the frame has a WCS.
    """
    try:
        return pixel_to_sky(header, x, y)
    except PickingError as exc:
        logger.debug("no sky coord for pixel (%s, %s): %s", x, y, exc)
        return None


# --- picks + aperture ------------------------------------------------------------

#: Default aperture radius, in pixels (matches the store's schema defaults).
DEFAULT_APERTURE_RADIUS_PX = 4.0
#: Default sky-annulus inner radius, in pixels.
DEFAULT_SKY_IN_PX = 8.0
#: Default sky-annulus outer radius, in pixels.
DEFAULT_SKY_OUT_PX = 12.0

#: Half-size of the snap window around a click, in pixels (a 9x9 window).
SNAP_WINDOW_PX = 4


@dataclass(frozen=True)
class ApertureParams:
    """The photometry aperture settings of a pick session, in pixels.

    - ``radius_px`` — the circular aperture radius (positive);
    - ``sky_in_px`` / ``sky_out_px`` — the sky annulus inner/outer radii
      (positive, ``sky_in_px < sky_out_px``).

    All fields are validated (and coerced to float) in ``__post_init__``;
    violations raise :class:`PickingError`.
    """

    radius_px: float = DEFAULT_APERTURE_RADIUS_PX
    sky_in_px: float = DEFAULT_SKY_IN_PX
    sky_out_px: float = DEFAULT_SKY_OUT_PX

    def __post_init__(self) -> None:
        radius = _finite_float(self.radius_px, "radius_px")
        inner = _finite_float(self.sky_in_px, "sky_in_px")
        outer = _finite_float(self.sky_out_px, "sky_out_px")
        if radius <= 0.0:
            raise PickingError(f"radius_px must be positive, got {self.radius_px!r}")
        if inner <= 0.0 or outer <= 0.0:
            raise PickingError(
                f"sky annulus radii must be positive, got sky_in_px={self.sky_in_px!r} "
                f"sky_out_px={self.sky_out_px!r}"
            )
        if not inner < outer:
            raise PickingError(
                f"sky_in_px ({inner}) must be smaller than sky_out_px ({outer})"
            )
        object.__setattr__(self, "radius_px", radius)
        object.__setattr__(self, "sky_in_px", inner)
        object.__setattr__(self, "sky_out_px", outer)


@dataclass(frozen=True)
class StarPickSpec:
    """One picked star of the current pick session.

    - ``role`` — ``"target"`` (the **target star**, GLOSSARY.md), ``"comp"``
      (a **comparison star**) or ``"check"`` (the **check star** — measured
      against the comp ensemble like the target but excluded from the
      ensemble; check-star-plot ticket 01);
    - ``x`` / ``y`` — the 0-based pixel position in the current image
      (``x`` = column, ``y`` = row; after snapping, a float centroid);
    - ``coord`` — the J2000 sky position
      (:class:`~seestar_controller.catalog.coords.J2000Coord`), or None when
      the pick is pixel-only (ticket 05: a frame without a usable WCS — e.g.
      a raw Seestar sub — is picked in pixel space only; also possible for
      picks loaded from the store without sky coords);
    - ``label`` — the user-given label (e.g. ``"TIC 123"``, ``"comp A"``), or
      None when unnamed.

    All fields are validated in ``__post_init__``; violations raise
    :class:`PickingError`.
    """

    role: str
    x: float
    y: float
    coord: J2000Coord | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise PickingError(f"role must be one of {ROLES}, got {self.role!r}")
        x = _finite_float(self.x, "x")
        y = _finite_float(self.y, "y")
        if self.coord is not None and not isinstance(self.coord, J2000Coord):
            raise PickingError(f"coord must be a J2000Coord or None, got {self.coord!r}")
        if self.label is not None and not isinstance(self.label, str):
            raise PickingError(f"label must be a string or None, got {self.label!r}")
        object.__setattr__(self, "x", x)
        object.__setattr__(self, "y", y)

    def describe(self) -> str:
        """The one-line label: ``"comp A — comp — (40.0, 30.0) RA 5.5h Dec +22.0°"``."""
        pos = f"({self.x:.1f}, {self.y:.1f})"
        sky = (
            f" RA {self.coord.ra_hours:.4f}h Dec {self.coord.dec_deg:+.4f}°"
            if self.coord is not None
            else ""
        )
        return f"{self.label or self.role} — {self.role} — {pos}{sky}"


@dataclass(frozen=True)
class PickSession:
    """The frozen snapshot of a :class:`PickController`, handed to the
    listeners on every change.

    - ``image`` — the current image (a
      :class:`~seestar_controller.enhance.fits_io.FitsImage`, duck-typed:
      ``.data`` / ``.header`` / ``.width`` / ``.height``), or None;
    - ``picks`` — the picks, in pick order (target first after a load — the
      store loads target-first, then the check star, then comps), as a
      tuple;
    - ``aperture`` — the session's :class:`ApertureParams`.
    """

    image: Any | None
    picks: tuple[StarPickSpec, ...]
    aperture: ApertureParams


#: A pick-session listener: called with a frozen :class:`PickSession` on any change.
PickListener = Callable[[PickSession], None]


def _snap_centroid(
    data: np.ndarray, x: float, y: float, window: int = SNAP_WINDOW_PX
) -> tuple[float, float]:
    """Snap ``(x, y)`` to the local brightness centroid inside a
    ``(2*window+1)`` square window (clipped to the image).

    Weights are ``clip(value - median(window), 0, None)`` over the finite
    pixels (RGB images are reduced to their channel mean first). When the
    window has no finite pixels, or its peak does not exceed its median (a
    flat window — no source), the exact ``(x, y)`` is returned.
    """
    plane = np.asarray(data, dtype=np.float64)
    if plane.ndim == 3:
        plane = plane[..., :3].mean(axis=2)
    height, width = plane.shape[0], plane.shape[1]
    cx = math.floor(x)
    cy = math.floor(y)
    x0 = max(0, cx - window)
    x1 = min(width, cx + window + 1)
    y0 = max(0, cy - window)
    y1 = min(height, cy + window + 1)
    win = plane[y0:y1, x0:x1]
    finite = np.isfinite(win)
    if not finite.any():
        return x, y
    values = win[finite]
    peak = float(values.max())
    background = float(np.median(values))
    if not peak > background:
        return x, y  # flat window: no source found -> exact pixel
    weights = np.where(finite, np.clip(win - background, 0.0, None), 0.0)
    total = float(weights.sum())
    if not total > 0.0:
        return x, y
    yy, xx = np.mgrid[y0:y1, x0:x1]
    snapped_x = float((weights * xx).sum() / total)
    snapped_y = float((weights * yy).sum() / total)
    return snapped_x, snapped_y


class PickController:
    """The app-level star-picking controller: the current image, the picks,
    the aperture settings, persistence into the project, and observer
    notification.

    - ``project`` — the store project the picks are saved to / loaded from
      (None = no project yet; :meth:`set_project` loads the stored picks);
    - ``viewer_controller`` — an optional
      :class:`~seestar_controller.enhance.viewer_controller.ViewerController`:
      the pick controller listens to it and adopts every newly opened stack's
      image as its current image (see :meth:`attach_viewer`).

    Operations: :meth:`pick_at` (click -> snap -> sky coord when the frame has
    a WCS -> pick; always works in pixel space, ticket 05),
    :meth:`add_at_sky` (sky -> WCS pixel -> pick; ticket 02),
    :meth:`remove_pick`, :meth:`set_role`, :meth:`clear`,
    :meth:`set_aperture` / :meth:`set_aperture_params`,
    :meth:`set_image` / :meth:`set_project` / :meth:`attach_viewer`,
    :meth:`save` / :meth:`load`. Every change notifies the listeners with a
    frozen :class:`PickSession`; listener exceptions are isolated (logged,
    never propagated), mirroring the enhance viewer controller.
    """

    def __init__(self, project: Any | None = None, viewer_controller: Any | None = None) -> None:
        self._project: Any | None = None
        self._viewer_controller: Any | None = None
        self._image: Any | None = None
        self._picks: list[StarPickSpec] = []
        self._aperture = ApertureParams()
        self._listeners: list[PickListener] = []
        if viewer_controller is not None:
            self.attach_viewer(viewer_controller)
        if project is not None:
            self.set_project(project)

    # -- reading ---------------------------------------------------------------

    @property
    def project(self) -> Any | None:
        """The current store project (None when not set yet)."""
        return self._project

    @property
    def viewer_controller(self) -> Any | None:
        """The viewer controller this pick controller follows (if any)."""
        return self._viewer_controller

    @property
    def image(self) -> Any | None:
        """The current image (the last stack opened on the viewer), or None."""
        return self._image

    @property
    def picks(self) -> tuple[StarPickSpec, ...]:
        """The picks, in pick order (a tuple snapshot)."""
        return tuple(self._picks)

    @property
    def target(self) -> StarPickSpec | None:
        """The target pick (None when not picked yet)."""
        return next((p for p in self._picks if p.role == ROLE_TARGET), None)

    @property
    def comps(self) -> tuple[StarPickSpec, ...]:
        """The comparison-star picks."""
        return tuple(p for p in self._picks if p.role == ROLE_COMP)

    @property
    def check(self) -> StarPickSpec | None:
        """The check-star pick (None when not picked yet)."""
        return next((p for p in self._picks if p.role == ROLE_CHECK), None)

    @property
    def aperture(self) -> ApertureParams:
        """The session's aperture settings."""
        return self._aperture

    @property
    def snapshot(self) -> PickSession:
        """The frozen :class:`PickSession` of the current state."""
        return PickSession(
            image=self._image, picks=tuple(self._picks), aperture=self._aperture
        )

    # -- listeners ---------------------------------------------------------------

    @property
    def listeners(self) -> tuple[PickListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    def add_listener(self, listener: PickListener) -> PickListener:
        """Register an observer callback (idempotent); returns it, so it can
        be used as a decorator. The listener receives a frozen
        :class:`PickSession` on any change."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: PickListener) -> None:
        """Unregister an observer callback (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self) -> None:
        """Hand a frozen snapshot to every listener; a failing listener is
        logged and isolated (never breaks the others or the caller)."""
        snapshot = self.snapshot
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:
                logger.exception("photometry pick listener failed")

    # -- image / project / viewer --------------------------------------------------

    def set_image(self, image: Any | None) -> None:
        """Set the current image (a FitsImage-like object: ``.data`` /
        ``.header`` / ``.width`` / ``.height``); None clears it. The picks are
        kept (they are project-level, positioned on the last picked image).
        Notifies the listeners."""
        self._image = image
        self._notify()

    def attach_viewer(self, viewer_controller: Any) -> None:
        """Follow ``viewer_controller``: register as its listener and adopt
        every newly opened stack's image as the current image (the last opened
        image is kept when the viewer clears). Re-attaching detaches the
        previous viewer first."""
        if self._viewer_controller is not None:
            self._viewer_controller.remove_listener(self._on_viewer_snapshot)
        self._viewer_controller = viewer_controller
        viewer_controller.add_listener(self._on_viewer_snapshot)
        image = getattr(viewer_controller, "current_image", None)
        if image is not None:
            self._image = image

    def _on_viewer_snapshot(self, snapshot: Any) -> None:
        """Viewer listener: a newly opened stack's image becomes current."""
        image = getattr(snapshot, "image", None)
        if image is not None and image is not self._image:
            self._image = image
            self._notify()

    def set_project(self, project: Any | None) -> None:
        """Set/replace the current project (None clears it and the picks) and
        load the project's stored picks (see :meth:`load`). Notifies."""
        self._project = project
        if project is None:
            self._picks = []
            self._notify()
            return
        self.load()

    # -- picking -------------------------------------------------------------------

    def _normalize_index(self, index: Any) -> int:
        """Validate an index into the picks (int, in range, negatives allowed)."""
        if isinstance(index, bool) or not isinstance(index, int):
            raise PickingError(f"index must be an int, got {index!r}")
        n = len(self._picks)
        if index < 0:
            index += n
        if not 0 <= index < n:
            raise PickingError(f"index {index} out of range for {n} picks")
        return index

    def _add_spec(self, spec: StarPickSpec) -> StarPickSpec:
        """Add ``spec`` with the role semantics: re-picking ``"target"`` or
        ``"check"`` REPLACES the existing pick of that role, comps
        accumulate. Notifies the listeners and returns the spec."""
        if spec.role in (ROLE_TARGET, ROLE_CHECK):
            self._picks = [p for p in self._picks if p.role != spec.role]
        self._picks.append(spec)
        self._notify()
        return spec

    def pick_at(
        self,
        x: float,
        y: float,
        role: str,
        *,
        label: str | None = None,
        snap: bool = True,
    ) -> StarPickSpec:
        """Pick a star at image pixel ``(x, y)`` (0-based) with ``role``.

        - the click must lie inside the current image (else
          :class:`PickingError`);
        - with ``snap=True`` (default) the click snaps to the local brightness
          centroid within a small window (see :func:`_snap_centroid`; the exact
          pixel is used when no source is found in the window);
        - the (snapped) pixel is converted to a J2000 coord via
          :func:`try_pixel_to_sky` **only when the frame has a usable 2D
          celestial WCS** (RGB stacks: the WCS of the 2D celestial subset, SIP
          handled — see :func:`_wcs_from_header`); without a WCS (a raw
          Seestar sub) the pick is pixel-only (``coord=None``) — picking
          ALWAYS works in pixel space (ticket 05);
        - the pick is added: re-picking ``"target"`` REPLACES the existing
          target pick, re-picking ``"check"`` replaces the existing check
          pick, comps accumulate.

        Returns the new :class:`StarPickSpec` and notifies the listeners.
        """
        if self._image is None:
            raise PickingError(
                "no image — open a stack first (the PickController follows the "
                "shared ViewerController, or set_image() directly)"
            )
        if role not in ROLES:
            raise PickingError(f"role must be one of {ROLES}, got {role!r}")
        px = _finite_float(x, "x")
        py = _finite_float(y, "y")
        data = np.asarray(self._image.data)
        height, width = int(data.shape[0]), int(data.shape[1])
        if not (0.0 <= px < width and 0.0 <= py < height):
            raise PickingError(
                f"click ({px}, {py}) is outside the {width}x{height} image"
            )
        if snap:
            sx, sy = _snap_centroid(data, px, py)
        else:
            sx, sy = px, py
        coord = try_pixel_to_sky(self._image.header, sx, sy)
        spec = StarPickSpec(role=role, x=sx, y=sy, coord=coord, label=label)
        return self._add_spec(spec)

    def add_at_sky(
        self,
        coord: Any,
        role: str = ROLE_COMP,
        *,
        label: str | None = None,
        snap: bool = False,
    ) -> StarPickSpec:
        """Add a pick at a SKY position (ticket 02 — e.g. a catalog standard
        star): the inverse of :meth:`pick_at`.

        - the sky position (a :class:`~seestar_controller.catalog.coords.J2000Coord`,
          a ``(ra_deg, dec_deg)`` pair, or a duck-typed object) is converted to
          a 0-based image pixel via :func:`sky_to_pixel` against the current
          image's WCS header — a frame WITHOUT a usable WCS (a raw Seestar
          sub) raises :class:`PickingError` here ("plate-solve the frame
          first"): a sky position cannot be mapped to a pixel without a WCS;
        - the pixel must lie inside the current image (else
          :class:`PickingError` — a catalog star outside the frame cannot be
          picked);
        - with ``snap=True`` the pixel snaps to the local brightness centroid
          (default False: a catalog position is already precise, so the exact
          WCS pixel is used);
        - the pick keeps the GIVEN sky coord (not re-derived from the pixel);
        - the pick is added with the usual role semantics: ``"target"`` or
          ``"check"`` replaces the existing pick of that role, comps
          accumulate (default role is ``"comp"``).

        Returns the new :class:`StarPickSpec` and notifies the listeners.
        """
        if self._image is None:
            raise PickingError(
                "no image — open a stack first (the PickController follows the "
                "shared ViewerController, or set_image() directly)"
            )
        if role not in ROLES:
            raise PickingError(f"role must be one of {ROLES}, got {role!r}")
        ra_deg, dec_deg = _ra_dec_deg(coord)
        sky = J2000Coord(ra_deg=ra_deg, dec_deg=dec_deg)
        x, y = sky_to_pixel(self._image.header, sky)
        data = np.asarray(self._image.data)
        height, width = int(data.shape[0]), int(data.shape[1])
        if not (0.0 <= x < width and 0.0 <= y < height):
            raise PickingError(
                f"sky position {sky} maps to pixel ({x:.2f}, {y:.2f}), outside "
                f"the {width}x{height} image — cannot add it as a pick"
            )
        if snap:
            x, y = _snap_centroid(data, x, y)
        spec = StarPickSpec(role=role, x=x, y=y, coord=sky, label=label)
        return self._add_spec(spec)

    def remove_pick(self, index: int) -> StarPickSpec:
        """Remove and return the pick at ``index`` (negatives allowed).
        Notifies."""
        idx = self._normalize_index(index)
        spec = self._picks.pop(idx)
        self._notify()
        return spec

    def set_role(self, index: int, role: str) -> StarPickSpec:
        """Change the role of the pick at ``index``; returns the updated pick.

        Setting the role to ``"target"`` or ``"check"`` replaces any other
        pick of that role (the one-target / one-check rule, same as
        :meth:`pick_at`). Notifies.
        """
        idx = self._normalize_index(index)
        if role not in ROLES:
            raise PickingError(f"role must be one of {ROLES}, got {role!r}")
        updated = replace(self._picks[idx], role=role)
        picks: list[StarPickSpec] = []
        for i, pick in enumerate(self._picks):
            if i == idx:
                picks.append(updated)
            elif role in (ROLE_TARGET, ROLE_CHECK) and pick.role == role:
                continue  # replaced by the new pick of that role
            else:
                picks.append(pick)
        self._picks = picks
        self._notify()
        return updated

    def clear(self) -> None:
        """Remove all picks. Notifies."""
        self._picks = []
        self._notify()

    # -- aperture --------------------------------------------------------------------

    def set_aperture(self, aperture: ApertureParams) -> ApertureParams:
        """Set the session aperture (must be an :class:`ApertureParams`);
        returns it. Notifies."""
        if not isinstance(aperture, ApertureParams):
            raise PickingError(
                f"aperture must be an ApertureParams, got {aperture!r}"
            )
        self._aperture = aperture
        self._notify()
        return aperture

    def set_aperture_params(
        self,
        *,
        radius_px: float | None = None,
        sky_in_px: float | None = None,
        sky_out_px: float | None = None,
    ) -> ApertureParams:
        """Update individual aperture fields (None keeps the current value);
        the combined result is validated (see :class:`ApertureParams`) and
        set. Returns the new :class:`ApertureParams`. Notifies.

        Raises :class:`PickingError` on an invalid combination — the previous
        aperture is left unchanged then.
        """
        current = self._aperture
        aperture = ApertureParams(
            radius_px=current.radius_px if radius_px is None else radius_px,
            sky_in_px=current.sky_in_px if sky_in_px is None else sky_in_px,
            sky_out_px=current.sky_out_px if sky_out_px is None else sky_out_px,
        )
        return self.set_aperture(aperture)

    # -- persistence -------------------------------------------------------------------

    @staticmethod
    def _spec_from_stored(pick: StarPick) -> StarPickSpec:
        """Map a stored :class:`~seestar_controller.store.stars.StarPick` to a
        :class:`StarPickSpec` (coord only when both ra/dec are present)."""
        coord = (
            J2000Coord(ra_deg=pick.ra_deg, dec_deg=pick.dec_deg)
            if pick.ra_deg is not None and pick.dec_deg is not None
            else None
        )
        return StarPickSpec(role=pick.role, x=pick.x, y=pick.y, coord=coord, label=pick.label)

    def _stored_from_spec(self, spec: StarPickSpec) -> StarPick:
        """Map a :class:`StarPickSpec` to a store
        :class:`~seestar_controller.store.stars.StarPick`, writing the
        session aperture into the per-pick aperture columns."""
        return StarPick(
            role=spec.role,
            label=spec.label,
            x=spec.x,
            y=spec.y,
            ra_deg=spec.coord.ra_deg if spec.coord is not None else None,
            dec_deg=spec.coord.dec_deg if spec.coord is not None else None,
            aperture_radius=self._aperture.radius_px,
            sky_annulus_inner=self._aperture.sky_in_px,
            sky_annulus_outer=self._aperture.sky_out_px,
        )

    def save(self, project: Any | None = None) -> list[StarPick]:
        """Save the current picks into ``project`` (default: the current
        project) via :func:`seestar_controller.store.stars.save_star_picks`
        (replace semantics; an empty pick list clears the stored picks).

        The session aperture is written into every pick row. Returns the saved
        :class:`~seestar_controller.store.stars.StarPick` list. Raises
        :class:`PickingError` when no project is set, the ensemble is invalid
        (e.g. comps without a target — the store requires exactly one target
        for a non-empty selection), or the store write fails (chained).
        """
        project = project if project is not None else self._project
        if project is None:
            raise PickingError(
                "no project — set one with set_project() first "
                "(the Projects view is a later ticket)"
            )
        try:
            stored = [self._stored_from_spec(spec) for spec in self._picks]
            return save_star_picks(project, stored)
        except StarPickError as exc:
            raise PickingError(
                f"cannot save star picks in project "
                f"{getattr(project, 'slug', project)!r}: {exc}"
            ) from exc

    def load(self, project: Any | None = None) -> tuple[StarPickSpec, ...]:
        """Load the stored picks of ``project`` (default: the current project)
        via :func:`seestar_controller.store.stars.load_star_picks` into the
        session (replacing the current picks) and adopt the aperture of the
        first stored pick (the target — the store loads target-first).

        Returns the loaded picks as a tuple of :class:`StarPickSpec`. Raises
        :class:`PickingError` when no project is set or the store read fails
        (chained). Notifies.
        """
        project = project if project is not None else self._project
        if project is None:
            raise PickingError(
                "no project — set one with set_project() first "
                "(the Projects view is a later ticket)"
            )
        try:
            stored = load_star_picks(project)
        except (StarPickError, ProjectError) as exc:
            raise PickingError(
                f"cannot load star picks of project "
                f"{getattr(project, 'slug', project)!r}: {exc}"
            ) from exc
        self._picks = [self._spec_from_stored(pick) for pick in stored]
        if stored:
            first = stored[0]
            self._aperture = ApertureParams(
                radius_px=first.aperture_radius,
                sky_in_px=first.sky_annulus_inner,
                sky_out_px=first.sky_annulus_outer,
            )
        self._notify()
        return tuple(self._picks)
