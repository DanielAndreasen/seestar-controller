"""The plan queue model — the PURE layer of :mod:`seestar_controller.observing`.

This module holds tonight's plan (GLOSSARY.md: **Plan (queue)** — the ordered
list of targets for one night) as plain data + operations, fully testable
headless: **no ``gi`` import, no link import** —
stdlib + :mod:`seestar_controller.catalog` only (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary). The widget
layer (:mod:`.queue_editor`) imports ``gi`` at module top and is never imported
by this module or eagerly by the package ``__init__``.

What lives here:

- :class:`QueueEntry` — one queued target: a **name or a coordinate** (exactly
  one of the two at construction; a name is unresolved until
  :func:`resolve_names` fills the coord from the catalog), plus the stacking
  parameters ``mosaic`` / ``exposure_s`` / ``gain``. All validation happens in
  ``__post_init__`` and raises :class:`QueueError` (a :class:`ValueError`)
  with a clear message. The numeric ranges mirror the link's stacking wire
  contract (the link's ``stacking`` module: exposure 1-60 s, gain
  0-100, mosaic pan/tilt 0-10 deg) — re-declared here because this package
  must not import the link (same re-declaration pattern as
  :mod:`seestar_controller.ui.state` uses for the config-dir constants);
- :class:`PlanQueue` — the ordered list: :meth:`~PlanQueue.add`,
  :meth:`~PlanQueue.remove`, :meth:`~PlanQueue.duplicate`,
  :meth:`~PlanQueue.move` / :meth:`~PlanQueue.move_up` / :meth:`~PlanQueue.move_down`,
  :meth:`~PlanQueue.clear`, ``len()`` / ``[]`` / the :attr:`~PlanQueue.entries`
  snapshot, and the observer list (:meth:`~PlanQueue.add_listener` /
  :meth:`~PlanQueue.remove_listener` — the same pattern as the ui controllers)
  notified with a frozen :class:`QueueEvent` after every change;
- :func:`resolve_names` — resolves every name-only entry through a
  ``resolver`` (default: a lazy :func:`~seestar_controller.catalog.facade.lookup_name`
  wrapper, brightest candidate first). Resolution **fills the coord** of the
  entry (the entry keeps its name for display — see :meth:`QueueEntry.resolved`);
  per-entry failures are collected into the returned :class:`ResolutionReport`
  and the entry stays unresolved (name kept, coord ``None``) — a bad name or a
  failing resolver never raises away the whole queue.

The queue lives for the session only: it is held in memory by the app-level
:class:`~.queue_controller.PlanQueueController` (not persisted to disk yet —
per the ticket).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from seestar_controller.catalog.coords import (
    CoordinateError,
    J2000Coord,
    parse_coords,
)

# --- stacking parameter ranges -------------------------------------------------
# Mirrored from the link's stacking module (link.stacking: exposure 1-60 s, gain 0-100,
# mosaic pan/tilt 0-10 deg — the mock's wire contract, unverified against real
# hardware). Re-declared here because this pure module must not import the
# link (same pattern as ui/state.py re-declares the config-dir constants).

#: Sub-frame exposure range, in seconds.
EXPOSURE_MIN_S = 1.0
EXPOSURE_MAX_S = 60.0
#: Gain range.
GAIN_MIN = 0
GAIN_MAX = 100
#: Mosaic framing (pan/tilt) range, in degrees.
MOSAIC_MIN_DEG = 0.0
MOSAIC_MAX_DEG = 10.0

#: Default sub-frame exposure (the 10 s sub-frame, GLOSSARY.md).
DEFAULT_EXPOSURE_S = 10.0
#: Default gain (middle of the 0-100 range).
DEFAULT_GAIN = 50
#: Default mosaic: off.
DEFAULT_MOSAIC: Mosaic = False


class QueueError(ValueError):
    """A queue validation/operation error — a :class:`ValueError` with a
    clear message (bad entry field, bad index, bad resolver result, ...)."""


#: A mosaic setting: ``False``/``True`` (off / on with the Scope's default
#: geometry) or a ``(pan, tilt)`` framing in degrees.
Mosaic = bool | tuple[float, float]


def normalize_mosaic(mosaic: Any) -> Mosaic:
    """Normalize a ``mosaic`` value to ``bool | (pan, tilt)``.

    Accepts a bool, ``None`` (-> ``False``), a ``(pan, tilt)`` pair, or a
    mapping with ``pan``/``tilt`` keys, in degrees. Raises :class:`QueueError`
    on anything else or out-of-range values (mirrors the link's
    ``normalize_mosaic`` ranges).
    """
    if mosaic is None:
        return False
    if isinstance(mosaic, bool):
        return mosaic
    if isinstance(mosaic, Mapping):
        pan_raw, tilt_raw = mosaic.get("pan", 0.0), mosaic.get("tilt", 0.0)
    elif isinstance(mosaic, (tuple, list)):
        if len(mosaic) != 2:
            raise QueueError(
                f"mosaic framing must be a (pan, tilt) pair in degrees, got {mosaic!r}"
            )
        pan_raw, tilt_raw = mosaic
    else:
        raise QueueError(
            f"mosaic must be a bool or a (pan, tilt) framing in degrees, got {mosaic!r}"
        )
    if isinstance(pan_raw, bool) or isinstance(tilt_raw, bool):
        raise QueueError(f"mosaic pan/tilt must be numbers in degrees, got {mosaic!r}")
    try:
        pan, tilt = float(pan_raw), float(tilt_raw)
    except (TypeError, ValueError):
        raise QueueError(
            f"mosaic pan/tilt must be numbers in degrees, got {mosaic!r}"
        ) from None
    for name, value in (("pan", pan), ("tilt", tilt)):
        if not math.isfinite(value) or not MOSAIC_MIN_DEG <= value <= MOSAIC_MAX_DEG:
            raise QueueError(
                f"mosaic {name} must be in "
                f"[{MOSAIC_MIN_DEG}, {MOSAIC_MAX_DEG}] degrees, got {value}"
            )
    return (pan, tilt)


def describe_mosaic(mosaic: Mosaic) -> str:
    """Human-readable mosaic for a queue row: ``"no mosaic"`` / ``"mosaic on"``
    / ``"mosaic 2°×1°"``."""
    if mosaic is True:
        return "mosaic on"
    if not mosaic:
        return "no mosaic"
    pan, tilt = mosaic
    return f"mosaic {pan:g}°×{tilt:g}°"


@dataclass(frozen=True)
class QueueEntry:
    """One queued target of tonight's plan.

    - ``name`` — a catalog name (e.g. ``"M 42"``), unresolved until
      :func:`resolve_names` fills ``coord``; or ``None`` when the entry was
      added by coordinates;
    - ``coord`` — the J2000 position (:class:`~seestar_controller.catalog.coords.J2000Coord`),
      or ``None`` while the name is unresolved;
    - ``mosaic`` — :data:`Mosaic` (bool or ``(pan, tilt)`` degrees);
    - ``exposure_s`` — sub-frame exposure in seconds, [1, 60];
    - ``gain`` — gain, [0, 100], stored as an ``int`` (the wire contract sends
      an int gain).

    Exactly one of ``name`` / ``coord`` must be set at construction — an entry
    holds a name OR a coord; a *resolved* entry (built via :meth:`resolved`)
    carries both (the name for display, the coord for the goto). All fields are
    validated in ``__post_init__``; violations raise :class:`QueueError`.
    """

    name: str | None = None
    coord: J2000Coord | None = None
    mosaic: Mosaic = DEFAULT_MOSAIC
    exposure_s: float = DEFAULT_EXPOSURE_S
    gain: int = DEFAULT_GAIN

    def __post_init__(self) -> None:
        if (self.name is None) == (self.coord is None):
            raise QueueError(
                "a queue entry needs exactly one of name or coord — "
                f"got name={self.name!r} coord={self.coord!r}"
            )
        if self.name is not None:
            if not isinstance(self.name, str) or not self.name.strip():
                raise QueueError(f"name must be a non-empty string, got {self.name!r}")
            object.__setattr__(self, "name", self.name.strip())
        if self.coord is not None and not isinstance(self.coord, J2000Coord):
            raise QueueError(f"coord must be a J2000Coord, got {self.coord!r}")
        object.__setattr__(self, "mosaic", normalize_mosaic(self.mosaic))
        if isinstance(self.exposure_s, bool) or not isinstance(self.exposure_s, (int, float)):
            raise QueueError(f"exposure_s must be a number of seconds, got {self.exposure_s!r}")
        exposure = float(self.exposure_s)
        if not math.isfinite(exposure) or not EXPOSURE_MIN_S <= exposure <= EXPOSURE_MAX_S:
            raise QueueError(
                f"exposure_s must be in [{EXPOSURE_MIN_S}, {EXPOSURE_MAX_S}] s, "
                f"got {self.exposure_s!r}"
            )
        object.__setattr__(self, "exposure_s", exposure)
        if isinstance(self.gain, bool) or not isinstance(self.gain, (int, float)):
            raise QueueError(f"gain must be a number, got {self.gain!r}")
        gain = float(self.gain)
        if not math.isfinite(gain) or not GAIN_MIN <= gain <= GAIN_MAX:
            raise QueueError(f"gain must be in [{GAIN_MIN}, {GAIN_MAX}], got {self.gain!r}")
        object.__setattr__(self, "gain", int(gain))

    @classmethod
    def resolved(
        cls,
        name: str,
        coord: J2000Coord,
        *,
        mosaic: Mosaic = DEFAULT_MOSAIC,
        exposure_s: float = DEFAULT_EXPOSURE_S,
        gain: int = DEFAULT_GAIN,
    ) -> QueueEntry:
        """Build a name entry whose coord has been resolved via the catalog.

        A resolved entry carries BOTH the name (display) and the coord (the
        goto target) — direct construction with both set is rejected by
        ``__post_init__`` (an unresolved entry must not claim a coord), so
        resolution goes through this classmethod.
        """
        if not isinstance(name, str) or not name.strip():
            raise QueueError(f"name must be a non-empty string, got {name!r}")
        if not isinstance(coord, J2000Coord):
            raise QueueError(f"coord must be a J2000Coord, got {coord!r}")
        entry = cls(
            coord=coord, mosaic=mosaic, exposure_s=exposure_s, gain=gain
        )  # validates coord/mosaic/exposure/gain (name=None passes exactly-one)
        object.__setattr__(entry, "name", name.strip())
        return entry

    @classmethod
    def _clone(cls, entry: QueueEntry) -> QueueEntry:
        """Copy an entry verbatim (bypassing validation — the source was
        already validated; needed because a resolved entry holds both name
        and coord, which ``__post_init__`` rejects)."""
        new = cls.__new__(cls)
        object.__setattr__(new, "name", entry.name)
        object.__setattr__(new, "coord", entry.coord)
        object.__setattr__(new, "mosaic", entry.mosaic)
        object.__setattr__(new, "exposure_s", entry.exposure_s)
        object.__setattr__(new, "gain", entry.gain)
        return new

    @property
    def is_resolved(self) -> bool:
        """Whether the entry has a coordinate (a name-only entry is unresolved)."""
        return self.coord is not None

    def describe(self) -> str:
        """The one-line row label: ``name (ra, dec) | mosaic | exp | gain``
        (an unresolved name shows ``(unresolved)``; a coord-only entry shows
        just the position)."""
        if self.name is not None and self.coord is not None:
            target = f"{self.name} (RA {self.coord.ra_hours:.4f}h Dec {self.coord.dec_deg:+.4f}°)"
        elif self.name is not None:
            target = f"{self.name} (unresolved)"
        else:
            assert self.coord is not None
            target = f"RA {self.coord.ra_hours:.4f}h Dec {self.coord.dec_deg:+.4f}°"
        return (
            f"{target} | {describe_mosaic(self.mosaic)} | "
            f"{self.exposure_s:g} s | gain {self.gain}"
        )

    def __str__(self) -> str:
        return self.describe()


# --- queue-changed events ---------------------------------------------------------

#: Event kind: an entry was appended.
EVENT_ADDED = "added"
#: Event kind: an entry was removed.
EVENT_REMOVED = "removed"
#: Event kind: an entry was duplicated.
EVENT_DUPLICATED = "duplicated"
#: Event kind: an entry changed position.
EVENT_MOVED = "moved"
#: Event kind: the queue was cleared.
EVENT_CLEARED = "cleared"
#: Event kind: a name was resolved to a coord (entry replaced in place).
EVENT_RESOLVED = "resolved"

#: All queue-changed event kinds.
QUEUE_EVENT_KINDS: tuple[str, ...] = (
    EVENT_ADDED,
    EVENT_REMOVED,
    EVENT_DUPLICATED,
    EVENT_MOVED,
    EVENT_CLEARED,
    EVENT_RESOLVED,
)


@dataclass(frozen=True)
class QueueEvent:
    """The queue-changed event handed to :class:`PlanQueue` listeners.

    - ``kind`` — one of :data:`QUEUE_EVENT_KINDS`;
    - ``index`` — the affected position (``None`` for ``cleared``);
    - ``entry`` — the affected entry (``None`` for ``cleared``);
    - ``to_index`` — the destination position for ``moved`` (else ``None``).
    """

    kind: str
    index: int | None = None
    entry: QueueEntry | None = None
    to_index: int | None = None


#: A queue-changed listener: called with a :class:`QueueEvent` after every change.
QueueListener = Callable[[QueueEvent], None]


class PlanQueue:
    """The ordered plan (queue) for one night — a list of :class:`QueueEntry`
    with editing operations and observer listeners.

    Operations: :meth:`add`, :meth:`remove`, :meth:`duplicate`, :meth:`move`
    / :meth:`move_up` / :meth:`move_down`, :meth:`clear`; reading: ``len(q)``,
    ``q[i]`` (negative indices work like a list), and the :attr:`entries`
    snapshot (a tuple — mutating the queue afterwards never changes an earlier
    snapshot). Every change notifies the listeners (in registration order)
    with a :class:`QueueEvent`; index/type errors raise :class:`QueueError`.
    """

    def __init__(self, entries: Iterable[QueueEntry] = ()) -> None:
        self._entries: list[QueueEntry] = []
        self._listeners: list[QueueListener] = []
        for entry in entries:
            self.add(entry)

    # -- reading ---------------------------------------------------------------

    @property
    def entries(self) -> tuple[QueueEntry, ...]:
        """A snapshot of the entries, in order (an immutable tuple)."""
        return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __getitem__(self, index: int) -> QueueEntry:
        return self._entries[self._normalize_index(index)]

    def __iter__(self):
        return iter(self.entries)

    def _normalize_index(self, index: Any) -> int:
        """Validate an index (int, in range, negatives allowed) -> normalized."""
        if isinstance(index, bool) or not isinstance(index, int):
            raise QueueError(f"index must be an int, got {index!r}")
        n = len(self._entries)
        if index < 0:
            index += n
        if not 0 <= index < n:
            raise QueueError(f"index {index} out of range for a queue of {n} entries")
        return index

    # -- listeners ---------------------------------------------------------------

    @property
    def listeners(self) -> tuple[QueueListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    def add_listener(self, listener: QueueListener) -> QueueListener:
        """Register an observer callback (idempotent); returns it, so it can
        be used as a decorator."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: QueueListener) -> None:
        """Unregister an observer callback (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self, event: QueueEvent) -> None:
        """Call every listener with ``event`` (same pattern as the ui controllers)."""
        for listener in list(self._listeners):
            listener(event)

    # -- editing -------------------------------------------------------------------

    def add(self, entry: QueueEntry) -> QueueEntry:
        """Append ``entry``; returns it. Raises :class:`QueueError` for a
        non-QueueEntry."""
        if not isinstance(entry, QueueEntry):
            raise QueueError(f"entry must be a QueueEntry, got {entry!r}")
        self._entries.append(entry)
        self._notify(QueueEvent(EVENT_ADDED, index=len(self._entries) - 1, entry=entry))
        return entry

    def remove(self, index: int) -> QueueEntry:
        """Remove and return the entry at ``index``."""
        index = self._normalize_index(index)
        entry = self._entries.pop(index)
        self._notify(QueueEvent(EVENT_REMOVED, index=index, entry=entry))
        return entry

    def duplicate(self, index: int) -> QueueEntry:
        """Insert a copy of the entry at ``index`` right after it; returns the copy."""
        index = self._normalize_index(index)
        copy = QueueEntry._clone(self._entries[index])
        self._entries.insert(index + 1, copy)
        self._notify(QueueEvent(EVENT_DUPLICATED, index=index + 1, entry=copy))
        return copy

    def move(self, index: int, to_index: int) -> None:
        """Move the entry at ``index`` to ``to_index`` (both validated; a
        no-op when they are equal — no event then)."""
        index = self._normalize_index(index)
        to_index = self._normalize_index(to_index)
        if index == to_index:
            return
        entry = self._entries.pop(index)
        self._entries.insert(to_index, entry)
        self._notify(QueueEvent(EVENT_MOVED, index=to_index, entry=entry, to_index=to_index))

    def move_up(self, index: int) -> bool:
        """Move the entry at ``index`` one position towards the front;
        returns whether it moved (``False`` at the front — no event then)."""
        index = self._normalize_index(index)
        if index == 0:
            return False
        self.move(index, index - 1)
        return True

    def move_down(self, index: int) -> bool:
        """Move the entry at ``index`` one position towards the back;
        returns whether it moved (``False`` at the end — no event then)."""
        index = self._normalize_index(index)
        if index == len(self._entries) - 1:
            return False
        self.move(index, index + 1)
        return True

    def clear(self) -> None:
        """Remove all entries (a no-op — no event — when already empty)."""
        if not self._entries:
            return
        self._entries.clear()
        self._notify(QueueEvent(EVENT_CLEARED))

    def _replace(self, index: int, entry: QueueEntry, kind: str) -> QueueEntry:
        """Replace the entry at ``index`` in place (used by :func:`resolve_names`);
        notifies with ``kind``. Returns the new entry."""
        index = self._normalize_index(index)
        if not isinstance(entry, QueueEntry):
            raise QueueError(f"entry must be a QueueEntry, got {entry!r}")
        self._entries[index] = entry
        self._notify(QueueEvent(kind, index=index, entry=entry))
        return entry


# --- name resolution ----------------------------------------------------------------

#: A name resolver: ``resolver(name) -> J2000Coord`` (or anything with
#: ``ra_deg``/``dec_deg``, normalized to a :class:`J2000Coord`).
NameResolver = Callable[[str], Any]


def default_name_resolver(name: str) -> J2000Coord:
    """The default resolver: a lazy wrapper around the catalog facade's
    :func:`~seestar_controller.catalog.facade.lookup_name` (imported at call
    time so importing this module stays light). The brightest candidate
    (first — SIMBAD results are brightest-first) wins. Raises whatever the
    catalog raises (:class:`~seestar_controller.catalog.simbad.NameNotFoundError`
    / ``CatalogError`` ...) — :func:`resolve_names` collects it per entry.
    """
    from seestar_controller.catalog.facade import lookup_name

    candidates = lookup_name(name)
    if not candidates:
        from seestar_controller.catalog.simbad import NameNotFoundError

        raise NameNotFoundError(f"no catalog object named {name!r}")
    return candidates[0].coord


def _as_coord(value: Any) -> J2000Coord:
    """Normalize a resolver result to a :class:`J2000Coord` (accepts a
    J2000Coord or any object with ``ra_deg``/``dec_deg``); raises
    :class:`QueueError` otherwise."""
    if isinstance(value, J2000Coord):
        return value
    ra = getattr(value, "ra_deg", None)
    dec = getattr(value, "dec_deg", None)
    if ra is not None and dec is not None:
        try:
            return J2000Coord(ra_deg=ra, dec_deg=dec)
        except CoordinateError as exc:
            raise QueueError(f"resolver returned an invalid coordinate: {exc}") from exc
    raise QueueError(
        f"resolver must return a J2000Coord (or an object with ra_deg/dec_deg), got {value!r}"
    )


@dataclass(frozen=True)
class ResolutionReport:
    """The outcome of :func:`resolve_names`.

    - ``resolved`` — ``(index, entry)`` pairs that got a coord (the entry now
      carries both name and coord);
    - ``errors`` — ``(index, message)`` pairs for entries that stayed
      unresolved (unknown name, resolver failure, bad resolver result) — the
      entry keeps its name and has ``coord is None``.
    """

    resolved: tuple[tuple[int, QueueEntry], ...] = ()
    errors: tuple[tuple[int, str], ...] = ()

    @property
    def ok(self) -> bool:
        """Whether every name-only entry resolved (no errors)."""
        return not self.errors


def resolve_names(
    queue: PlanQueue, resolver: NameResolver | None = None
) -> ResolutionReport:
    """Resolve every name-only entry of ``queue`` through ``resolver``.

    ``resolver`` defaults to :func:`default_name_resolver` (the lazy catalog
    ``lookup_name`` wrapper) and is injectable for tests. For each entry with
    a name and no coord yet, the resolver is called; on success the entry is
    replaced in place (same index) by a :meth:`QueueEntry.resolved` copy —
    name kept for display, coord filled — and a ``resolved`` event is
    notified. On ANY per-entry failure (unknown name, resolver raising, bad
    resolver result) the error is collected into the report and the entry is
    left untouched (name kept, still unresolved) — the whole queue is never
    raised away. Entries that already have a coord are skipped.

    Returns the :class:`ResolutionReport`.
    """
    if not isinstance(queue, PlanQueue):
        raise QueueError(f"queue must be a PlanQueue, got {queue!r}")
    resolver = resolver if resolver is not None else default_name_resolver
    resolved: list[tuple[int, QueueEntry]] = []
    errors: list[tuple[int, str]] = []
    for index in range(len(queue)):
        entry = queue[index]
        if entry.name is None or entry.coord is not None:
            continue  # coord-only entry, or already resolved
        try:
            coord = _as_coord(resolver(entry.name))
        except Exception as exc:  # noqa: BLE001
            errors.append((index, f"{entry.name!r}: {type(exc).__name__}: {exc}"))
            continue
        new_entry = QueueEntry.resolved(
            entry.name,
            coord,
            mosaic=entry.mosaic,
            exposure_s=entry.exposure_s,
            gain=entry.gain,
        )
        queue._replace(index, new_entry, EVENT_RESOLVED)
        resolved.append((index, new_entry))
    return ResolutionReport(resolved=tuple(resolved), errors=tuple(errors))


def parse_entry_coords(ra: Any, dec: Any) -> J2000Coord:
    """Parse an ``(ra, dec)`` pair into a :class:`J2000Coord` via the catalog's
    :func:`~seestar_controller.catalog.coords.parse_coords` (sexagesimal or
    decimal; RA in hours, Dec in degrees). Raises :class:`QueueError` (wrapping
    the catalog's :class:`~seestar_controller.catalog.coords.CoordinateError`)
    on invalid input."""
    try:
        return parse_coords(ra, dec)
    except CoordinateError as exc:
        raise QueueError(f"invalid coordinates: {exc}") from exc
