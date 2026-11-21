"""The night-timeline model — the PURE layer behind the timeline view (ticket 02).

This is part of the PURE layer of :mod:`seestar_controller.visibility`: **no
``gi`` import** — stdlib + :mod:`numpy` + :mod:`astropy` + the sibling pure
modules only — so it is fully unit-testable headless (the sandbox has no GTK,
see docs/wiki/assumptions.md; a subprocess test enforces the boundary). No
network either (the planner's builtin ephemeris + bundled IERS-B, see
:mod:`.planner`).

What lives here (the data the timeline plot renders, all computed by
:mod:`.planner`):

- :class:`TimelineModel` — the frozen model of one night: the site, the night
  date, the night window, the x-axis (hours since local noon of the night, so
  the window always spans ``0..24``), one :class:`~.planner.AltitudeCurve` per
  RESOLVED queue entry (with the queue index it came from), the
  :class:`~.planner.TwilightTimes`, the :class:`~.planner.MoonTrack`, the
  suggested queue order, and a display label per queue entry;
- :func:`build_timeline` — build the model from the queued entries (anything
  with a ``coord``, e.g. the observing plan-queue entries), the site (a
  :class:`~.site.Site` or a settings mapping) and the night date. Unresolved
  entries (no coordinate yet) simply get no curve and are deferred to the end
  of the suggested order (that is :func:`~.planner.suggest_order`'s contract);
- :func:`reorder_moves` — the sequence of ``(from_index, to_index)`` moves
  that reorders a list into a given permutation using only
  ``list.pop``/``insert`` semantics — i.e. exactly the
  :meth:`~seestar_controller.observing.queue.PlanQueue.move` contract, so the
  widget can apply the suggestion through the queue controller's public API
  without touching the observing package;
- :func:`apply_order` — apply a suggested order to a queue-controller-like
  object (duck-typed: ``len()`` / ``move()`` — e.g. the observing
  ``PlanQueueController``) via :func:`reorder_moves`; returns the number of
  moves performed;
- display helpers: :func:`axis_hours` (hours since the window start, for the
  plot x-axis), :func:`format_local` (an instant as local ``HH:MM`` at the
  site) and :func:`suggested_labels` (the entry labels in suggested order).

The matplotlib Agg rendering of the model lives in :mod:`.render` (also pure);
the GTK widget is :mod:`.timeline_view` (ticket 02's widget layer, ``gi`` at
module top, lazy-exported from the package).
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import astropy.units as u
import numpy as np
from astropy.time import Time

from seestar_controller.visibility.planner import (
    DEFAULT_STEP_MINUTES,
    AltitudeCurve,
    MoonTrack,
    TwilightTimes,
    _entry_label,
    _try_entry_coord,
    altitude_curves,
    moon_track,
    night_grid,
    night_window,
    suggest_order,
    twilight_times,
)
from seestar_controller.visibility.site import Site, VisibilityError

#: Maximum queue length accepted by :func:`build_timeline` / :func:`apply_order`
#: (a sanity cap — a plan is a handful of targets, not thousands).
MAX_TIMELINE_ENTRIES = 1000


@dataclass(frozen=True)
class TimelineModel:
    """The frozen night-timeline model: everything the plot needs.

    - ``site`` / ``night`` — the observing site and the night date (local);
    - ``window`` — the night window ``(start, end)`` (astropy
      :class:`~astropy.time.Time`, UTC): local noon → local noon + 1 day;
    - ``axis_hours`` — the x-axis: hours since the window start, ``0..24``
      (numpy array, one entry per grid point);
    - ``curves`` — one :class:`~.planner.AltitudeCurve` per RESOLVED entry, in
      queue order (``curve_indices[i]`` is the queue index of ``curves[i]``);
    - ``twilight`` — the astronomical twilight times of the night;
    - ``moon`` — the moon track (altitude/azimuth + illuminated fraction);
    - ``order`` — the suggested queue order (indices into the entries);
    - ``labels`` — a display label per queue entry (queue order).
    """

    site: Site
    night: datetime.date
    window: tuple[Time, Time]
    axis_hours: np.ndarray
    curves: tuple[AltitudeCurve, ...]
    curve_indices: tuple[int, ...]
    twilight: TwilightTimes
    moon: MoonTrack
    order: tuple[int, ...]
    labels: tuple[str, ...]


def _as_site(site: Site | Mapping[str, Any]) -> Site:
    """Normalize ``site`` to a :class:`Site` (a settings mapping goes through
    :meth:`Site.from_settings`)."""
    if isinstance(site, Site):
        return site
    if isinstance(site, Mapping):
        return Site.from_settings(site)
    raise VisibilityError(f"site must be a Site or a settings mapping, got {site!r}")


def _as_night(night: Any) -> datetime.date:
    """Normalize ``night`` to a :class:`datetime.date` (a ``datetime`` uses its
    date part); raises :class:`VisibilityError` otherwise."""
    if isinstance(night, datetime.datetime):
        return night.date()
    if isinstance(night, datetime.date):
        return night
    raise VisibilityError(
        f"night must be a datetime.date (the local date of the observing "
        f"night), got {night!r}"
    )


def axis_hours(window: tuple[Time, Time], times: Time) -> np.ndarray:
    """The x-axis of the timeline: hours since the window start (``0..24``)
    for the obstime grid ``times`` (an astropy :class:`~astropy.time.Time`)."""
    start = window[0]
    return np.asarray((times - start).to_value(u.hour), dtype=float)


def format_local(site: Site, time: Time) -> str:
    """An instant as local ``HH:MM`` at the site (for axis tick labels and
    twilight annotations)."""
    return site.local_time(time).strftime("%H:%M")


def build_timeline(
    entries: Iterable[Any],
    site: Site | Mapping[str, Any],
    night: Any,
    *,
    step_minutes: float = DEFAULT_STEP_MINUTES,
) -> TimelineModel:
    """Build the :class:`TimelineModel` for the queued ``entries``.

    ``entries`` are the plan-queue entries (anything with a ``coord`` holding
    a :class:`~seestar_controller.catalog.coords.J2000Coord`) or plain
    ``J2000Coord`` objects — the same duck-typing as
    :func:`~.planner.altitude_curves` / :func:`~.planner.suggest_order`.
    Unresolved entries (no coordinate yet) get no curve and are deferred to
    the end of the suggested order; a bad coord raises
    :class:`VisibilityError` (via :func:`~.planner.altitude_curves`).
    """
    site = _as_site(site)
    night = _as_night(night)
    entries = list(entries)
    if len(entries) > MAX_TIMELINE_ENTRIES:
        raise VisibilityError(
            f"too many entries for a timeline ({len(entries)} > {MAX_TIMELINE_ENTRIES})"
        )
    labels = tuple(_entry_label(entry) for entry in entries)
    resolved = [
        (index, entry)
        for index, entry in enumerate(entries)
        if _try_entry_coord(entry) is not None
    ]
    window = night_window(site, night)
    grid = night_grid(site, night, step_minutes)
    curves = altitude_curves(
        [entry for _, entry in resolved], site, night, step_minutes=step_minutes
    )
    return TimelineModel(
        site=site,
        night=night,
        window=window,
        axis_hours=axis_hours(window, grid),
        curves=curves,
        curve_indices=tuple(index for index, _ in resolved),
        twilight=twilight_times(site, night),
        moon=moon_track(site, night, step_minutes=step_minutes),
        order=suggest_order(entries, site, night, step_minutes=step_minutes),
        labels=labels,
    )


def suggested_labels(model: TimelineModel) -> tuple[str, ...]:
    """The entry labels in the suggested order (the hint list the view shows
    next to the Apply button)."""
    return tuple(model.labels[index] for index in model.order)


def reorder_moves(order: Iterable[int], length: int) -> tuple[tuple[int, int], ...]:
    """The ``(from_index, to_index)`` moves that reorder a list of ``length``
    items into the permutation ``order``, using only pop/insert semantics —
    exactly :meth:`~seestar_controller.observing.queue.PlanQueue.move`
    (``move(index, to_index)`` pops at ``index`` and inserts at ``to_index``).

    The moves are computed selection-style: for each target position, the
    entry that belongs there is moved from its current position. An identity
    permutation yields no moves. ``order`` must be a permutation of
    ``range(length)`` — otherwise :class:`VisibilityError`.
    """
    if isinstance(length, bool) or not isinstance(length, int):
        raise VisibilityError(f"length must be an int, got {length!r}")
    if length < 0:
        raise VisibilityError(f"length must be >= 0, got {length!r}")
    order = tuple(order)
    if len(order) != length or sorted(order) != list(range(length)):
        raise VisibilityError(
            f"order must be a permutation of range({length}), got {order!r}"
        )
    current = list(range(length))  # current[position] = original index
    moves: list[tuple[int, int]] = []
    for target_pos, original in enumerate(order):
        position = current.index(original)
        if position != target_pos:
            moves.append((position, target_pos))
            current.pop(position)
            current.insert(target_pos, original)
    return tuple(moves)


def apply_order(queue: Any, order: Iterable[int]) -> int:
    """Apply the suggested ``order`` to ``queue`` — a queue-controller-like
    object exposing ``len()`` and ``move(index, to_index)`` (the observing
    :class:`~seestar_controller.observing.queue_controller.PlanQueueController`
    / :class:`~seestar_controller.observing.queue.PlanQueue` public API) — via
    :func:`reorder_moves`. Returns the number of moves performed (0 when the
    queue already matches the suggestion).

    Each move notifies the queue's listeners (a ``moved`` event), so every
    observer (the queue editor, this timeline view) re-renders. Raises
    :class:`VisibilityError` when ``order`` is not a permutation of
    ``range(len(queue))``.
    """
    if not hasattr(queue, "move") or not hasattr(queue, "__len__"):
        raise VisibilityError(
            f"queue must expose len() and move(index, to_index), got {queue!r}"
        )
    length = len(queue)
    moves = reorder_moves(order, length)
    for from_index, to_index in moves:
        queue.move(from_index, to_index)
    return len(moves)


#: A settings source: a mapping, or a zero-arg callable returning one (the
#: live ``AppState.settings`` of the app).
SettingsSource = Callable[[], Mapping[str, Any]] | Mapping[str, Any]


def resolve_settings(source: SettingsSource | None) -> Mapping[str, Any] | None:
    """Resolve a :data:`SettingsSource` to the current settings mapping (a
    callable is called); ``None`` stays ``None``."""
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source
    if callable(source):
        result = source()
        return result if isinstance(result, Mapping) else None
    return None
