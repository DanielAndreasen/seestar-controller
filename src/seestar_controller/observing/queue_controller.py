"""Pure-Python plan-queue controller for the observing package (ticket 01).

This is a PURE module of :mod:`seestar_controller.observing`: **no ``gi``
import, no link import** — stdlib + catalog only —
so it is fully unit-testable headless (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary). The widget
layer (:mod:`.queue_editor`) imports ``gi`` at module top and is never imported
by this module or eagerly by the package ``__init__``.

:class:`PlanQueueController` wraps a :class:`~.queue.PlanQueue` plus a name
``resolver`` and is the object the app owns (one shared instance, like the ui
:class:`~seestar_controller.ui.connection.ConnectionController`) and the
Observe view's queue editor drives:

- :meth:`add_entry` / :meth:`add_by_name` / :meth:`add_by_coords` /
  :meth:`add_resolved` — build a validated :class:`~.queue.QueueEntry` and
  append it (``add_by_coords`` parses RA/Dec through the catalog's
  ``parse_coords`` and wraps :class:`~seestar_controller.catalog.coords.CoordinateError`
  into :class:`~.queue.QueueError`);
- :meth:`remove` / :meth:`duplicate` / :meth:`move` / :meth:`move_up` /
  :meth:`move_down` / :meth:`clear` — delegate to the queue;
- :meth:`resolve_name` — resolve ONE name (the editor's Resolve button):
  returns the :class:`~seestar_controller.catalog.coords.J2000Coord` or raises
  :class:`~.queue.QueueError` with the failure message;
- :meth:`resolve_names` — resolve every unresolved entry of the queue through
  the resolver (see :func:`~.queue.resolve_names`); returns the
  :class:`~.queue.ResolutionReport`;
- ``add_listener`` / ``remove_listener`` — the controller re-notifies its own
  listeners with the queue's :class:`~.queue.QueueEvent`\\ s (the queue's
  listener list is an implementation detail; observers subscribe here).

The queue is held in memory for the session only (not persisted to disk yet —
per the ticket).
"""

from __future__ import annotations

from typing import Any

from seestar_controller.catalog.coords import J2000Coord

from .queue import (
    DEFAULT_EXPOSURE_S,
    DEFAULT_GAIN,
    DEFAULT_MOSAIC,
    Mosaic,
    PlanQueue,
    QueueEntry,
    QueueError,
    QueueEvent,
    QueueListener,
    ResolutionReport,
    NameResolver,
    _as_coord,
    default_name_resolver,
    parse_entry_coords,
    resolve_names,
)


class PlanQueueController:
    """The app-level plan-queue controller: a :class:`~.queue.PlanQueue` +
    a name resolver + observer forwarding.

    - ``queue`` — the wrapped queue (default: a fresh empty one);
    - ``resolver`` — the name resolver (default:
      :func:`~.queue.default_name_resolver`, the lazy catalog wrapper);
      injectable for tests.
    """

    def __init__(
        self,
        queue: PlanQueue | None = None,
        *,
        resolver: NameResolver | None = None,
    ) -> None:
        self._queue = queue if queue is not None else PlanQueue()
        self._resolver: NameResolver = resolver if resolver is not None else default_name_resolver
        self._listeners: list[QueueListener] = []
        self._queue.add_listener(self._on_queue_event)

    # -- accessors ---------------------------------------------------------------

    @property
    def queue(self) -> PlanQueue:
        """The wrapped :class:`~.queue.PlanQueue`."""
        return self._queue

    @property
    def resolver(self) -> NameResolver:
        """The name resolver used by :meth:`resolve_name` / :meth:`resolve_names`."""
        return self._resolver

    @property
    def entries(self) -> tuple[QueueEntry, ...]:
        """A snapshot of the queued entries, in order."""
        return self._queue.entries

    def __len__(self) -> int:
        return len(self._queue)

    def __getitem__(self, index: int) -> QueueEntry:
        return self._queue[index]

    # -- listeners -----------------------------------------------------------------

    @property
    def listeners(self) -> tuple[QueueListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    def add_listener(self, listener: QueueListener) -> QueueListener:
        """Register an observer callback (idempotent); returns it, so it can
        be used as a decorator. The listener receives the queue's
        :class:`~.queue.QueueEvent`\\ s."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: QueueListener) -> None:
        """Unregister an observer callback (no-op when not registered)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _on_queue_event(self, event: QueueEvent) -> None:
        """Forward a queue event to the controller's own listeners."""
        for listener in list(self._listeners):
            listener(event)

    # -- adding entries ----------------------------------------------------------------

    def add_entry(self, entry: QueueEntry) -> QueueEntry:
        """Append an already-built :class:`~.queue.QueueEntry`; returns it."""
        return self._queue.add(entry)

    def add_by_name(
        self,
        name: str,
        *,
        mosaic: Mosaic = DEFAULT_MOSAIC,
        exposure_s: float = DEFAULT_EXPOSURE_S,
        gain: int = DEFAULT_GAIN,
    ) -> QueueEntry:
        """Add an entry by catalog ``name`` (unresolved until
        :meth:`resolve_names`); returns the new entry."""
        return self.add_entry(
            QueueEntry(name=name, mosaic=mosaic, exposure_s=exposure_s, gain=gain)
        )

    def add_by_coords(
        self,
        ra: Any,
        dec: Any,
        *,
        mosaic: Mosaic = DEFAULT_MOSAIC,
        exposure_s: float = DEFAULT_EXPOSURE_S,
        gain: int = DEFAULT_GAIN,
    ) -> QueueEntry:
        """Add an entry by RA/Dec (parsed via the catalog's ``parse_coords`` —
        sexagesimal or decimal, RA in hours / Dec in degrees). Invalid input
        raises :class:`~.queue.QueueError`; returns the new entry."""
        coord = parse_entry_coords(ra, dec)
        return self.add_entry(
            QueueEntry(coord=coord, mosaic=mosaic, exposure_s=exposure_s, gain=gain)
        )

    def add_resolved(
        self,
        name: str,
        coord: J2000Coord,
        *,
        mosaic: Mosaic = DEFAULT_MOSAIC,
        exposure_s: float = DEFAULT_EXPOSURE_S,
        gain: int = DEFAULT_GAIN,
    ) -> QueueEntry:
        """Add an entry whose name was already resolved to ``coord`` (the
        editor's Resolve-then-Add flow); returns the new entry."""
        return self.add_entry(
            QueueEntry.resolved(
                name, coord, mosaic=mosaic, exposure_s=exposure_s, gain=gain
            )
        )

    # -- editing ------------------------------------------------------------------------

    def remove(self, index: int) -> QueueEntry:
        """Remove and return the entry at ``index``."""
        return self._queue.remove(index)

    def duplicate(self, index: int) -> QueueEntry:
        """Duplicate the entry at ``index`` right after it; returns the copy."""
        return self._queue.duplicate(index)

    def move(self, index: int, to_index: int) -> None:
        """Move the entry at ``index`` to ``to_index``."""
        self._queue.move(index, to_index)

    def move_up(self, index: int) -> bool:
        """Move the entry at ``index`` one position up; returns whether it moved."""
        return self._queue.move_up(index)

    def move_down(self, index: int) -> bool:
        """Move the entry at ``index`` one position down; returns whether it moved."""
        return self._queue.move_down(index)

    def clear(self) -> None:
        """Remove all entries."""
        self._queue.clear()

    # -- name resolution ------------------------------------------------------------------

    def resolve_name(self, name: str) -> J2000Coord:
        """Resolve ONE ``name`` through the resolver (the editor's Resolve
        button); returns the :class:`J2000Coord`. Raises
        :class:`~.queue.QueueError` with the failure message on an empty name,
        a resolver failure, or a bad resolver result."""
        if not isinstance(name, str) or not name.strip():
            raise QueueError(f"name must be a non-empty string, got {name!r}")
        query = name.strip()
        try:
            return _as_coord(self._resolver(query))
        except QueueError:
            raise
        except Exception as exc:
            raise QueueError(
                f"could not resolve {query!r}: {type(exc).__name__}: {exc}"
            ) from exc

    def resolve_names(self) -> ResolutionReport:
        """Resolve every unresolved (name-only) entry of the queue; returns
        the :class:`~.queue.ResolutionReport` (per-entry failures are collected
        there, never raised)."""
        return resolve_names(self._queue, self._resolver)
