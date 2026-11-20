"""Catalog assist for comparison stars — the PURE layer of photometry ticket 02
(:mod:`seestar_controller.photometry.suggest`).

Given the **target pick's** sky coordinates, list the nearby standard stars
from the catalog module (:func:`seestar_controller.catalog.facade.Catalog.lookup_standards`)
and let the user add one as a **comparison star (comp)** pick: the suggestion's
sky position is converted to a pixel with
:func:`~seestar_controller.photometry.picking.sky_to_pixel` against the current
frame's WCS and added via
:meth:`~seestar_controller.photometry.picking.PickController.add_at_sky`
(ticket-01 semantics kept: comps accumulate, one target).

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) — stdlib +
:mod:`seestar_controller.catalog` + :mod:`seestar_controller.photometry.picking`
only. The catalog facade itself is imported **lazily** inside
:func:`default_standards_lookup` (the default ``lookup`` callable), and tests
inject a mocked callable, so no test ever touches the network or the catalog's
HTTP boundary.

What lives here:

- :class:`SuggestionError` — a :class:`~.picking.PhotometryError` (hence a
  :class:`ValueError`): a bad radius, a bad suggestion index, no pick
  controller/image to add a comp to, a duplicate comp, or a failed
  sky->pixel conversion while adding;
- :class:`CompSuggestion` — one frozen suggestion snapshot (name, J2000 coord,
  magnitude, band, distance from the target in degrees);
- :class:`SuggestionSnapshot` — the frozen controller snapshot handed to the
  listeners: the target coord, the configured radius, the suggestions tuple,
  and a clean ``error`` string (catalog failures land HERE, never as a
  traceback);
- :func:`default_standards_lookup` — the default injectable catalog callable
  (lazy :class:`~seestar_controller.catalog.facade.Catalog`);
- :class:`SuggestionController` — takes the shared
  :class:`~.picking.PickController` (it follows the target pick), an
  injectable ``lookup`` callable and the configured ``radius_deg`` (the pure
  layer is settings-agnostic: the app injects the value read from the
  settings, see :func:`seestar_controller.ui.settings.comp_search_radius_deg`).
  :meth:`~SuggestionController.refresh` runs the cone search (radius-filtered,
  nearest-first), :meth:`~SuggestionController.add_suggestion` adds one as a
  comp pick, :meth:`~SuggestionController.set_radius` changes the configured
  radius (re-searching when a target is set), and listeners get frozen
  :class:`SuggestionSnapshot`\\ s on every change (idempotent registration,
  isolated listener exceptions — the same pattern as
  :class:`~.picking.PickController`).

Conventions (documented choices):

- **The radius is authoritative here too:** the catalog already filters its
  cone server- and client-side, but the controller re-filters every returned
  star with :func:`~seestar_controller.catalog.coords.angular_distance`
  (``distance <= radius``, RA-0 seam safe) and sorts the suggestions
  nearest-first — so a mocked/foreign ``lookup`` cannot leak out-of-radius
  stars.
- **Catalog failures are a snapshot error state, not an exception:**
  :meth:`~SuggestionController.refresh` catches ANY exception from the
  ``lookup`` callable (e.g. :class:`~seestar_controller.catalog.facade.CatalogUnavailableError`
  when offline with no cache) and stores a clean one-line message in
  :attr:`SuggestionSnapshot.error` with an empty suggestion list; the
  controller stays usable and the next :meth:`~SuggestionController.refresh`
  recovers. No traceback ever escapes.
- **No WCS on the frame -> a "plate-solve first" hint, not an error (ticket
  05):** raw Seestar subs have no celestial WCS, so a target picked on them
  is pixel-only (``coord=None``). Catalog assist needs the target's SKY
  position, so :meth:`~SuggestionController.refresh` then skips the catalog
  lookup entirely and surfaces :data:`NO_WCS_HINT` in
  :attr:`SuggestionSnapshot.error` — a clear hint tying into the astrometry
  spec, never an exception; picking itself is unaffected
  (:meth:`~seestar_controller.photometry.picking.PickController.pick_at`
  always works in pixel space).
- **Blocking call:** :meth:`~SuggestionController.refresh` runs the catalog
  lookup inline (network, up to the catalog timeout) — call it off the GLib
  main loop; the widget layer (:mod:`.suggestions_panel`) runs it on a worker
  thread and marshals the listener notifications back with
  :func:`GLib.idle_add`.
- **Adding a suggestion:** :meth:`~SuggestionController.add_suggestion` converts
  the suggestion's sky coord to a pixel via
  :func:`~seestar_controller.photometry.picking.sky_to_pixel` against the
  current frame WCS (through
  :meth:`~seestar_controller.photometry.picking.PickController.add_at_sky`,
  ``snap=False`` by default — a catalog position is already precise) and adds
  it as a ``"comp"`` pick with the star's name as label. A suggestion whose
  position maps outside the frame, or that is already a comp, raises
  :class:`SuggestionError`.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from seestar_controller.catalog.coords import J2000Coord, angular_distance
from seestar_controller.store.stars import ROLE_COMP

from .picking import (
    PhotometryError,
    PickController,
    PickSession,
    StarPickSpec,
)

logger = logging.getLogger(__name__)


class SuggestionError(PhotometryError):
    """Any catalog-assist failure — a :class:`~.picking.PhotometryError` (hence
    a :class:`ValueError`) with a clear message: a bad radius, a bad
    suggestion index, no pick controller / image to add a comp to, a duplicate
    comp, or a sky->pixel conversion that failed or landed outside the frame."""


#: Default comp-search radius, in degrees (mirrors the settings default,
#: ``ui.state.DEFAULT_COMP_SEARCH_RADIUS_DEG`` — the app injects the configured
#: value; this is the fallback for standalone use).
DEFAULT_COMP_SEARCH_RADIUS_DEG = 1.5

#: Largest comp-search radius accepted, in degrees (mirrors the catalog's cone
#: limit, ``catalog.standards.MAX_RADIUS_DEG``).
MAX_COMP_SEARCH_RADIUS_DEG = 180.0

#: Tolerance (degrees) when re-filtering the catalog result against the radius,
#: so a star exactly ON the cone edge is not lost to floating-point drift (the
#: WCS round-trip alone contributes ~1e-6 deg of noise).
_RADIUS_EPSILON_DEG = 1e-6

#: Two sky positions closer than this (degrees) are the same star — used to
#: reject adding the same suggestion twice as a comp.
_SAME_STAR_EPSILON_DEG = 1e-9

#: The hint surfaced in :attr:`SuggestionSnapshot.error` when the target pick
#: has no sky coordinates because the frame has no usable WCS (a raw Seestar
#: sub — ticket 05): catalog assist needs a plate-solved frame.
NO_WCS_HINT = (
    "the target pick has no sky coordinates — this frame has no WCS (no "
    "celestial CTYPE cards); plate-solve it first (astrometry) to use catalog "
    "assist"
)


#: The injectable catalog callable: ``lookup(coord, radius_deg) -> sequence of
#: standard-star-like objects`` (anything with ``name`` / ``coord`` /
#: ``magnitude`` / ``band`` attributes — e.g.
#: :class:`~seestar_controller.catalog.standards.StandardStar`).
LookupFn = Callable[[Any, float], Sequence[Any]]

#: The injectable search runner: ``search_runner()`` is called instead of
#: running :meth:`SuggestionController.refresh` inline when a search is
#: triggered automatically (target pick changed, radius changed with a target
#: set). Default None -> run inline (synchronous). The widget layer sets this
#: to its worker-thread search, so the blocking catalog lookup never runs on
#: the GLib main loop.
SearchRunner = Callable[[], Any]


def default_standards_lookup(coord: Any, radius_deg: float) -> Sequence[Any]:
    """The default ``lookup`` callable: the catalog facade's cached,
    offline-graceful
    :meth:`~seestar_controller.catalog.facade.Catalog.lookup_standards`.

    The facade is imported INSIDE the function, so importing this pure module
    never instantiates a :class:`~seestar_controller.catalog.facade.Catalog`
    or touches the network; failures raise the catalog taxonomy
    (:class:`~seestar_controller.catalog.facade.CatalogUnavailableError`
    etc.), which :meth:`SuggestionController.refresh` turns into a clean
    snapshot error state.
    """
    from seestar_controller.catalog.facade import Catalog

    return Catalog().lookup_standards(coord, radius_deg)


def _validate_radius(value: Any) -> float:
    """Validate a comp-search radius (degrees): a real number (not a bool, not
    a string) in ``(0, MAX_COMP_SEARCH_RADIUS_DEG]``; returns the float.
    Raises :class:`SuggestionError` otherwise."""
    if isinstance(value, (bool, str)):
        raise SuggestionError(f"radius_deg must be a number, got {value!r}")
    try:
        radius = float(value)
    except (TypeError, ValueError):
        raise SuggestionError(f"radius_deg must be a number, got {value!r}") from None
    if not math.isfinite(radius) or not 0.0 < radius <= MAX_COMP_SEARCH_RADIUS_DEG:
        raise SuggestionError(
            f"radius_deg must be in (0, {MAX_COMP_SEARCH_RADIUS_DEG:g}] degrees, got {value!r}"
        )
    return radius


@dataclass(frozen=True)
class CompSuggestion:
    """One frozen catalog suggestion: a standard star near the target.

    - ``name`` — the catalog name (e.g. ``"APASS 12345678"``);
    - ``coord`` — the J2000 position;
    - ``magnitude`` — the star's magnitude in ``band`` (None when unknown);
    - ``band`` — the photometric band of ``magnitude`` (e.g. ``"V"``);
    - ``distance_deg`` — the great-circle distance from the target, in degrees.
    """

    name: str
    coord: J2000Coord
    magnitude: float | None
    band: str
    distance_deg: float

    def describe(self) -> str:
        """The one-line label: ``"APASS 12345678 — V 12.34 — 0.042° away"``."""
        mag = f"{self.band} {self.magnitude:.2f}" if self.magnitude is not None else f"{self.band} ?"
        return f"{self.name} — {mag} — {self.distance_deg:.4f}° away"


@dataclass(frozen=True)
class SuggestionSnapshot:
    """The frozen snapshot of a :class:`SuggestionController`, handed to the
    listeners on every change.

    - ``target`` — the target pick's J2000 coord (None when no target is
      picked yet);
    - ``radius_deg`` — the configured comp-search radius;
    - ``suggestions`` — the suggestions, nearest-first, as a tuple;
    - ``error`` — a clean one-line message when the assist cannot run: the
      last catalog lookup failed (e.g. offline with no cache), OR the target
      pick has no sky coordinates because the frame has no WCS
      (:data:`NO_WCS_HINT` — plate-solve first, ticket 05); None when
      healthy — never a traceback.
    """

    target: J2000Coord | None
    radius_deg: float
    suggestions: tuple[CompSuggestion, ...]
    error: str | None


#: A suggestion listener: called with a frozen :class:`SuggestionSnapshot` on
#: any change.
SuggestListener = Callable[[SuggestionSnapshot], None]


def _star_coord(star: Any) -> J2000Coord | None:
    """Extract a :class:`J2000Coord` from a standard-star-like object (its
    ``coord`` attribute, or ``ra_deg``/``dec_deg``); None when unusable."""
    coord = getattr(star, "coord", None)
    if isinstance(coord, J2000Coord):
        return coord
    ra = getattr(star, "ra_deg", None)
    dec = getattr(star, "dec_deg", None)
    if ra is not None and dec is not None:
        try:
            return J2000Coord(ra_deg=float(ra), dec_deg=float(dec))
        except Exception:  # CoordinateError is a ValueError; be lenient with fakes  # noqa: BLE001
            return None
    if isinstance(coord, (tuple, list)) and len(coord) == 2:
        try:
            return J2000Coord(ra_deg=float(coord[0]), dec_deg=float(coord[1]))
        except (TypeError, ValueError):
            return None
    return None


class SuggestionController:
    """The catalog-assist controller: standard-star suggestions around the
    target pick, and adding them as comp picks.

    - ``picker_controller`` — the shared
      :class:`~seestar_controller.photometry.picking.PickController`; the
      suggestion controller follows it (listens to its snapshots) and
      re-searches whenever the target pick changes (its sky coord, or a
      pixel-only target appearing on a frame without WCS — ticket 05). May
      be None (standalone) and attached later with :meth:`attach_picker`.
    - ``lookup`` — the injectable catalog callable ``lookup(coord,
      radius_deg) -> sequence of standard stars`` (default:
      :func:`default_standards_lookup`, the lazy catalog facade). Tests mock
      this — no network.
    - ``radius_deg`` — the configured comp-search radius in degrees
      (default :data:`DEFAULT_COMP_SEARCH_RADIUS_DEG`); the pure layer is
      settings-agnostic — the app injects the value read from the settings.
    - ``search_runner`` — optional :data:`SearchRunner`: called instead of
      running :meth:`refresh` inline when a search is triggered automatically
      (target pick changed, radius changed with a target set). Default None
      -> inline (synchronous). The widget layer sets its worker-thread search
      here, so the blocking catalog lookup never runs on the GLib main loop.
      Settable via the :attr:`search_runner` property.

    Operations: :meth:`refresh` (run the cone search for the current target —
    synchronous/blocking, call it off the main loop), :meth:`add_suggestion`
    (add suggestion ``index`` as a comp pick), :meth:`set_radius`,
    :meth:`attach_picker`. Every change notifies the listeners with a frozen
    :class:`SuggestionSnapshot`; listener exceptions are isolated (logged,
    never propagated), mirroring :class:`~.picking.PickController`.
    """

    def __init__(
        self,
        picker_controller: PickController | None = None,
        *,
        lookup: LookupFn | None = None,
        radius_deg: float = DEFAULT_COMP_SEARCH_RADIUS_DEG,
        search_runner: SearchRunner | None = None,
    ) -> None:
        self._picker: PickController | None = None
        self._lookup: LookupFn = lookup if lookup is not None else default_standards_lookup
        self._radius = _validate_radius(radius_deg)
        self._search_runner: SearchRunner | None = search_runner
        self._target: J2000Coord | None = None
        self._has_target: bool = False
        self._suggestions: tuple[CompSuggestion, ...] = ()
        self._error: str | None = None
        self._listeners: list[SuggestListener] = []
        if picker_controller is not None:
            self.attach_picker(picker_controller)

    # -- reading ---------------------------------------------------------------

    @property
    def picker_controller(self) -> PickController | None:
        """The pick controller this suggestion controller follows (if any)."""
        return self._picker

    @property
    def radius_deg(self) -> float:
        """The configured comp-search radius, in degrees."""
        return self._radius

    @property
    def search_runner(self) -> SearchRunner | None:
        """The injectable search runner used for automatic searches (None ->
        :meth:`refresh` runs inline). Settable — the widget layer sets its
        worker-thread search here."""
        return self._search_runner

    @search_runner.setter
    def search_runner(self, runner: SearchRunner | None) -> None:
        self._search_runner = runner

    @property
    def target(self) -> J2000Coord | None:
        """The current target coord (None when no target is picked)."""
        return self._target

    @property
    def suggestions(self) -> tuple[CompSuggestion, ...]:
        """The suggestions, nearest-first (a tuple snapshot)."""
        return self._suggestions

    @property
    def error(self) -> str | None:
        """The last catalog failure as a clean message (None when healthy)."""
        return self._error

    @property
    def snapshot(self) -> SuggestionSnapshot:
        """The frozen :class:`SuggestionSnapshot` of the current state."""
        return SuggestionSnapshot(
            target=self._target,
            radius_deg=self._radius,
            suggestions=self._suggestions,
            error=self._error,
        )

    # -- listeners ---------------------------------------------------------------

    @property
    def listeners(self) -> tuple[SuggestListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    def add_listener(self, listener: SuggestListener) -> SuggestListener:
        """Register an observer callback (idempotent); returns it, so it can
        be used as a decorator. The listener receives a frozen
        :class:`SuggestionSnapshot` on any change."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: SuggestListener) -> None:
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
                logger.exception("photometry suggestion listener failed")

    # -- picker following ----------------------------------------------------------

    def attach_picker(self, picker_controller: PickController) -> None:
        """Follow ``picker_controller``: register as its listener and
        re-search whenever the target pick changes (via the
        :attr:`search_runner` when set, else inline) — also once immediately
        (inline) when a target is already picked. Re-attaching detaches the
        previous picker first."""
        if self._picker is not None:
            self._picker.remove_listener(self._on_pick_session)
        self._picker = picker_controller
        picker_controller.add_listener(self._on_pick_session)
        if self._target_state(picker_controller) != (self._has_target, self._target):
            self.refresh()

    def detach_picker(self) -> None:
        """Stop following the pick controller (the suggestions are kept)."""
        if self._picker is not None:
            self._picker.remove_listener(self._on_pick_session)
            self._picker = None

    @staticmethod
    def _target_of(picker: PickController | None) -> J2000Coord | None:
        """The target pick's sky coord of ``picker`` (None when absent)."""
        if picker is None:
            return None
        target = picker.target
        return target.coord if target is not None else None

    @classmethod
    def _target_state(cls, picker: PickController | None) -> tuple[bool, J2000Coord | None]:
        """The ``(has_target, coord)`` state of ``picker``: a pixel-only target
        pick (no WCS on the frame, ticket 05) counts as a target with coord
        None — distinct from no target at all."""
        if picker is None:
            return (False, None)
        target = picker.target
        if target is None:
            return (False, None)
        return (True, target.coord)

    def _on_pick_session(self, session: PickSession) -> None:
        """Pick-controller listener: re-search when the target pick changed
        (coord changed, a target appeared/disappeared, or the target became
        pixel-only — no WCS on the frame)."""
        target_pick = next((p for p in session.picks if p.role == "target"), None)
        state = (True, target_pick.coord) if target_pick is not None else (False, None)
        if state != (self._has_target, self._target):
            self._request_search()

    def _request_search(self) -> None:
        """Trigger a search: via the injectable :attr:`search_runner` when set
        (the widget layer's worker thread), else inline via :meth:`refresh`."""
        if self._search_runner is not None:
            self._search_runner()
        else:
            self.refresh()

    # -- radius ----------------------------------------------------------------------

    def set_radius(self, radius_deg: float) -> float:
        """Set the configured comp-search radius (validated, see
        :func:`_validate_radius`); returns it. When a target is set the search
        is re-run with the new radius immediately (via the :attr:`search_runner`
        when set), otherwise the listeners are just notified. Notifies."""
        radius = _validate_radius(radius_deg)
        if radius == self._radius:
            return radius
        self._radius = radius
        if self._has_target:
            self._request_search()
        else:
            self._notify()
        return radius

    # -- searching ---------------------------------------------------------------------

    def refresh(self) -> SuggestionSnapshot:
        """Run the catalog cone search for the current target pick and update
        the state; returns the new frozen :class:`SuggestionSnapshot`.

        - no target picked (or no picker attached) -> empty suggestions,
          ``target=None``, no error;
        - a target picked WITHOUT sky coordinates (the frame has no usable
          WCS — a raw Seestar sub, ticket 05) -> empty suggestions and
          :data:`NO_WCS_HINT` in :attr:`SuggestionSnapshot.error` ("plate-solve
          first"); the catalog ``lookup`` is NOT called and nothing raises;
        - the ``lookup`` callable is called as ``lookup(target, radius_deg)``;
          ANY exception it raises (e.g.
          :class:`~seestar_controller.catalog.facade.CatalogUnavailableError`
          offline with no cache) is caught and surfaced as a clean one-line
          :attr:`SuggestionSnapshot.error` with an empty suggestion list —
          never a traceback, and the controller stays usable;
        - the returned stars are re-filtered against the radius
          (:func:`~seestar_controller.catalog.coords.angular_distance`) and
          sorted nearest-first; malformed entries (no usable coord) are
          skipped.

        Notifies the listeners. NOTE: blocks on the catalog lookup — call off
        the main loop.
        """
        has_target, coord = self._target_state(self._picker)
        self._has_target = has_target
        self._target = coord
        if coord is None:
            self._suggestions = ()
            # a pixel-only target (no WCS on the frame) gets the plate-solve
            # hint; no target at all stays a clean empty state (ticket 05)
            self._error = NO_WCS_HINT if has_target else None
            self._notify()
            return self.snapshot
        target = coord
        try:
            stars = self._lookup(target, self._radius)
        except Exception as exc:  # catalog taxonomy included; never BaseException  # noqa: BLE001
            logger.warning("catalog comp lookup failed: %s", exc)
            self._suggestions = ()
            self._error = f"catalog lookup failed: {exc}"
            self._notify()
            return self.snapshot
        suggestions: list[CompSuggestion] = []
        for star in stars or ():
            coord = _star_coord(star)
            if coord is None:
                logger.warning("skipping malformed catalog star %r (no coord)", star)
                continue
            distance = angular_distance(target, coord)
            if distance > self._radius + _RADIUS_EPSILON_DEG:
                continue  # outside the configured radius
            name = getattr(star, "name", None)
            if not name:
                name = f"RA {coord.ra_hours:.4f}h Dec {coord.dec_deg:+.4f}°"
            magnitude = getattr(star, "magnitude", None)
            if magnitude is not None:
                try:
                    magnitude = float(magnitude)
                except (TypeError, ValueError):
                    magnitude = None
            band = getattr(star, "band", None) or "V"
            suggestions.append(
                CompSuggestion(
                    name=str(name),
                    coord=coord,
                    magnitude=magnitude,
                    band=str(band),
                    distance_deg=distance,
                )
            )
        suggestions.sort(key=lambda s: (s.distance_deg, s.name))
        self._suggestions = tuple(suggestions)
        self._error = None
        self._notify()
        return self.snapshot

    # -- adding a suggestion as a comp ---------------------------------------------------

    def _normalize_index(self, index: Any) -> int:
        """Validate an index into the suggestions (int, in range, negatives
        allowed); raises :class:`SuggestionError`."""
        if isinstance(index, bool) or not isinstance(index, int):
            raise SuggestionError(f"index must be an int, got {index!r}")
        n = len(self._suggestions)
        if index < 0:
            index += n
        if not 0 <= index < n:
            raise SuggestionError(f"index {index} out of range for {n} suggestions")
        return index

    def add_suggestion(self, index: int) -> StarPickSpec:
        """Add the suggestion at ``index`` as a COMP pick; returns the new
        :class:`~seestar_controller.photometry.picking.StarPickSpec`.

        The suggestion's sky coord is converted to a 0-based pixel via
        :func:`~seestar_controller.photometry.picking.sky_to_pixel` against the
        current frame's WCS (through
        :meth:`~seestar_controller.photometry.picking.PickController.add_at_sky`,
        ``snap=False`` — a catalog position is already precise) and added with
        the star's name as label; comps accumulate (ticket-01 semantics).

        Raises :class:`SuggestionError` when there is no pick controller, no
        image, the position maps outside the frame, the index is bad, or the
        star is already a comp pick (the underlying
        :class:`~.picking.PickingError` is chained).
        """
        idx = self._normalize_index(index)
        suggestion = self._suggestions[idx]
        if self._picker is None:
            raise SuggestionError(
                "no pick controller attached — attach_picker() first (the app "
                "wires the shared PickController)"
            )
        for pick in self._picker.comps:
            if pick.coord is not None and pick.coord.separation(suggestion.coord) < _SAME_STAR_EPSILON_DEG:
                raise SuggestionError(
                    f"{suggestion.name!r} is already a comp pick — not added twice"
                )
        try:
            return self._picker.add_at_sky(
                suggestion.coord, role=ROLE_COMP, label=suggestion.name, snap=False
            )
        except PhotometryError as exc:
            raise SuggestionError(
                f"cannot add suggestion {suggestion.name!r} as a comp: {exc}"
            ) from exc
