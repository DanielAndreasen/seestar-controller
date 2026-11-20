"""The visibility planner core — altitude curves, twilight, moon, queue order.

This is the PURE core of :mod:`seestar_controller.visibility` (the
visibility-planner spec, ``.scratch/visibility-planner/spec.md``): **no
``gi`` import anywhere in this package** — stdlib + :mod:`numpy` +
:mod:`astropy` + :mod:`seestar_controller.catalog` only — so it is fully
unit-testable headless (the sandbox has no GTK, see docs/wiki/assumptions.md;
a subprocess test enforces the boundary). No network either: the sun/moon
positions come from astropy's builtin ephemeris
(:func:`astropy.coordinates.get_body`) and the earth-rotation (UT1) data from
the bundled IERS-B table — importing this module sets
``astropy.utils.iers.conf.auto_download = False`` so a computation can never
trigger a network fetch (the test suite blocks sockets to prove it).

What lives here (given the observing site, the night date, and the resolved
plan-queue entries):

- the night window / obstime grid: :func:`night_window` and
  :func:`night_grid` — the night of date ``night`` at the site runs from
  **local noon** of ``night`` to **local noon** of ``night + 1`` (in the
  site's timezone, converted to UTC for astropy), so both the evening and
  the morning astronomical twilight of that night always fall inside the
  window, at any latitude;
- :func:`altitude_curves` — per-target altitude/azimuth curves over the
  night grid: astropy :class:`~astropy.coordinates.AltAz` on the grid for
  each entry's J2000 coordinate, returned as :class:`AltitudeCurve` (times,
  alt, az, plus the culmination ``transit_time`` / ``max_alt``);
- :func:`twilight_times` — the astronomical twilight start (evening dusk,
  sun crossing −18° downwards) and end (morning dawn, crossing upwards) as
  :class:`TwilightTimes`; ``None`` when the sun never reaches −18° that
  night (polar day/night);
- :func:`moon_track` — the moon's altitude/azimuth over the night plus the
  illuminated fraction (phase) from the sun–moon elongation, as
  :class:`MoonTrack`;
- :func:`suggest_order` — the greedy suggested queue order: each target's
  best altitude window is centred on its culmination, so the greedy pass
  repeatedly takes the remaining target whose window peaks earliest
  (transit time), breaking ties by the existing queue order — the result is
  stable w.r.t. the input order. Returns the entry indices in suggested
  order; unresolved entries (no coordinate yet) are deferred to the end,
  keeping their relative order.

Entries are the observing plan-queue entries
(:class:`~seestar_controller.observing.queue.QueueEntry`) — anything with a
``coord`` attribute holding a
:class:`~seestar_controller.catalog.coords.J2000Coord` — or plain
:class:`~seestar_controller.catalog.coords.J2000Coord` objects.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import astropy.units as u
import numpy as np
from astropy.coordinates import AltAz, SkyCoord, get_body
from astropy.time import Time
from astropy.utils import iers

from seestar_controller.catalog.coords import J2000Coord
from seestar_controller.visibility.site import Site, VisibilityError

# The planner is offline by design: sun/moon come from astropy's builtin
# ephemeris and UT1 from the bundled IERS-B table. Disabling auto_download at
# import guarantees a computation never triggers a network fetch (the tests
# block sockets to prove it) and keeps results reproducible.
iers.conf.auto_download = False

#: Sun altitude marking astronomical twilight, in degrees (IAU convention).
ASTRO_TWILIGHT_ALT_DEG = -18.0

#: Default obstime-grid step, in minutes.
DEFAULT_STEP_MINUTES = 5.0

#: The night window spans local noon -> local noon (+1 day), in hours.
NIGHT_WINDOW_HOURS = 24.0

#: Grid step used internally to bracket the twilight crossings, in minutes.
_TWILIGHT_GRID_MINUTES = 1.0

#: Bisection iterations when refining a twilight crossing (2^-60 of a
#: 1-minute bracket — far below a millisecond).
_CROSSING_ITERATIONS = 60


# --- night window + obstime grid -------------------------------------------------


def _as_night(night: Any) -> datetime.date:
    """Normalize the ``night`` argument to a :class:`datetime.date`.

    Accepts a ``date`` or a ``datetime`` (the date part is used); raises
    :class:`VisibilityError` otherwise.
    """
    if isinstance(night, datetime.datetime):
        return night.date()
    if isinstance(night, datetime.date):
        return night
    raise VisibilityError(
        f"night must be a datetime.date (the local date of the observing "
        f"night), got {night!r}"
    )


def _as_site(site: Any) -> Site:
    """Normalize the ``site`` argument to a :class:`Site` (a settings mapping
    is accepted too and goes through :meth:`Site.from_settings`)."""
    if isinstance(site, Site):
        return site
    if isinstance(site, Mapping):
        return Site.from_settings(site)
    raise VisibilityError(f"site must be a Site or a settings mapping, got {site!r}")


def night_window(site: Site | Mapping[str, Any], night: Any) -> tuple[Time, Time]:
    """The night window as ``(start, end)`` astropy :class:`~astropy.time.Time`
    (UTC): local noon of ``night`` to local noon of ``night + 1`` at the site.

    Local-noon-to-local-noon (not midnight-to-midnight) so that both the
    evening and the morning astronomical twilight of the night fall inside
    the window at any latitude and any DST shift.
    """
    site = _as_site(site)
    night = _as_night(night)
    tz = site.tzinfo()
    start_local = datetime.datetime(night.year, night.month, night.day, 12, 0, tzinfo=tz)
    end_local = start_local + datetime.timedelta(days=1)  # wall-clock +1 day (DST-safe)
    utc = datetime.UTC
    return Time(start_local.astimezone(utc)), Time(end_local.astimezone(utc))


def night_grid(
    site: Site | Mapping[str, Any],
    night: Any,
    step_minutes: float = DEFAULT_STEP_MINUTES,
) -> Time:
    """The obstime grid over the night window: ``start + k * step_minutes``
    for ``k = 0..N`` inclusive of both window ends (an astropy
    :class:`~astropy.time.Time`, UTC)."""
    site = _as_site(site)
    night = _as_night(night)
    if isinstance(step_minutes, bool) or not isinstance(step_minutes, (int, float)):
        raise VisibilityError(f"step_minutes must be a number, got {step_minutes!r}")
    step = float(step_minutes)
    if not np.isfinite(step) or not 0.0 < step <= NIGHT_WINDOW_HOURS * 60.0:
        raise VisibilityError(
            f"step_minutes must be in (0, {NIGHT_WINDOW_HOURS * 60.0:g}] minutes, got {step!r}"
        )
    start, end = night_window(site, night)
    total_minutes = (end - start).to_value(u.min)
    count = round(total_minutes / step) + 1
    return start + np.arange(count) * step * u.min


# --- entries -----------------------------------------------------------------------


def _entry_label(entry: Any) -> str:
    """A display label for an entry: its name, else its coordinate, else repr."""
    name = getattr(entry, "name", None)
    if isinstance(name, str) and name.strip():
        return name
    coord = getattr(entry, "coord", None)
    if isinstance(coord, J2000Coord):
        return str(coord)
    if isinstance(entry, J2000Coord):
        return str(entry)
    return repr(entry)


def _entry_coord(entry: Any) -> J2000Coord:
    """The resolved J2000 coordinate of an entry; raises
    :class:`VisibilityError` for an unresolved entry or a bad coord."""
    coord = getattr(entry, "coord", None)
    if coord is None and isinstance(entry, J2000Coord):
        coord = entry
    if coord is None:
        raise VisibilityError(
            f"entry {_entry_label(entry)!r} has no resolved coordinate — "
            "resolve the queue names first (observing.resolve_names)"
        )
    if not isinstance(coord, J2000Coord):
        raise VisibilityError(
            f"entry {_entry_label(entry)!r}: coord must be a J2000Coord, got {coord!r}"
        )
    return coord


def _try_entry_coord(entry: Any) -> J2000Coord | None:
    """Like :func:`_entry_coord` but returns ``None`` for unresolved entries."""
    coord = getattr(entry, "coord", None)
    if coord is None and isinstance(entry, J2000Coord):
        coord = entry
    if coord is None:
        return None
    if not isinstance(coord, J2000Coord):
        raise VisibilityError(
            f"entry {_entry_label(entry)!r}: coord must be a J2000Coord, got {coord!r}"
        )
    return coord


# --- altitude curves -----------------------------------------------------------------


@dataclass(frozen=True)
class AltitudeCurve:
    """One target's altitude/azimuth curve over the night.

    - ``name`` — the entry label (queue-entry name, else the coordinate);
    - ``times`` — the obstime grid (astropy :class:`~astropy.time.Time`, UTC);
    - ``alt`` / ``az`` — altitude / azimuth on the grid
      (:class:`~astropy.units.Quantity`, degrees; az in ``[0, 360)``);
    - ``transit_time`` — the grid time of the culmination (max altitude);
    - ``max_alt`` — the altitude at culmination (degrees).
    """

    name: str
    times: Time
    alt: u.Quantity
    az: u.Quantity
    transit_time: Time
    max_alt: u.Quantity


def altitude_curves(
    entries: Iterable[Any],
    site: Site | Mapping[str, Any],
    night: Any,
    *,
    step_minutes: float = DEFAULT_STEP_MINUTES,
) -> tuple[AltitudeCurve, ...]:
    """Per-target ``(times, alt, az)`` curves over the night, one
    :class:`AltitudeCurve` per entry, in the input order.

    ``entries`` are the plan-queue entries (anything with a ``coord`` holding
    a :class:`~seestar_controller.catalog.coords.J2000Coord`) or plain
    ``J2000Coord`` objects. Every entry must be resolved — an unresolved
    entry raises :class:`VisibilityError` (planning a target without a
    position is meaningless; :func:`suggest_order` is the tolerant variant).
    """
    site = _as_site(site)
    night = _as_night(night)
    entries = list(entries)
    coords = [_entry_coord(entry) for entry in entries]
    times = night_grid(site, night, step_minutes)
    frame = AltAz(obstime=times, location=site.location)
    curves: list[AltitudeCurve] = []
    for entry, coord in zip(entries, coords, strict=True):
        sky = SkyCoord(ra=coord.ra_deg * u.deg, dec=coord.dec_deg * u.deg, frame="icrs")
        altaz = sky.transform_to(frame)
        alt = altaz.alt.to(u.deg)
        az = altaz.az.to(u.deg)
        peak = int(np.argmax(alt.to_value(u.deg)))
        curves.append(
            AltitudeCurve(
                name=_entry_label(entry),
                times=times,
                alt=alt,
                az=az,
                transit_time=times[peak],
                max_alt=alt[peak],
            )
        )
    return tuple(curves)


# --- astronomical twilight -------------------------------------------------------------


@dataclass(frozen=True)
class TwilightTimes:
    """The astronomical twilight of one night at the site.

    - ``night`` — the night date (local date at the site);
    - ``astro_start`` — evening astronomical dusk: the sun crossing −18°
      downwards (an astropy :class:`~astropy.time.Time`, UTC), or ``None``
      when the sun never reaches −18° within the night window (polar
      day/night — there is no astronomical twilight that night);
    - ``astro_end`` — morning astronomical dawn: the sun crossing −18°
      upwards, or ``None`` likewise.
    """

    night: datetime.date
    astro_start: Time | None
    astro_end: Time | None


def _sun_alt_deg(time: Time, site: Site) -> float:
    """The sun's apparent altitude at one instant, in degrees."""
    frame = AltAz(obstime=time, location=site.location)
    return float(get_body("sun", time, site.location).transform_to(frame).alt.to_value(u.deg))


def _refine_crossing(
    func: Any, t_lo: Time, t_hi: Time, iterations: int = _CROSSING_ITERATIONS
) -> Time:
    """Bisect a sign change of ``func`` (``func(t) -> float``) between
    ``t_lo`` / ``t_hi``; returns the crossing time."""
    f_lo = func(t_lo)
    for _ in range(iterations):
        mid = t_lo + (t_hi - t_lo) / 2
        f_mid = func(mid)
        if f_mid == 0.0:
            return mid
        if (f_lo < 0.0) == (f_mid < 0.0):
            t_lo, f_lo = mid, f_mid
        else:
            t_hi = mid
    return t_lo + (t_hi - t_lo) / 2


def twilight_times(site: Site | Mapping[str, Any], night: Any) -> TwilightTimes:
    """The astronomical twilight start/end of the night at the site.

    The sun's altitude is sampled on a 1-minute grid over the night window;
    every crossing of :data:`ASTRO_TWILIGHT_ALT_DEG` (−18°) is refined by
    bisection. The first downward crossing is the evening dusk
    (``astro_start``), the first upward crossing the morning dawn
    (``astro_end``); both are ``None`` when the sun never reaches −18° that
    night.
    """
    site = _as_site(site)
    night = _as_night(night)
    grid = night_grid(site, night, _TWILIGHT_GRID_MINUTES)
    frame = AltAz(obstime=grid, location=site.location)
    sun_alt = get_body("sun", grid, site.location).transform_to(frame).alt.to_value(u.deg)
    f = sun_alt - ASTRO_TWILIGHT_ALT_DEG
    target = ASTRO_TWILIGHT_ALT_DEG

    def sun_offset(t: Time) -> float:
        return _sun_alt_deg(t, site) - target

    astro_start: Time | None = None
    astro_end: Time | None = None
    for i in range(len(grid) - 1):
        f_lo, f_hi = float(f[i]), float(f[i + 1])
        if f_lo == 0.0:
            crossing, going_down = grid[i], f_hi < 0.0
        elif f_lo * f_hi < 0.0:
            crossing = _refine_crossing(sun_offset, grid[i], grid[i + 1])
            going_down = f_lo > 0.0
        else:
            continue
        if going_down:
            if astro_start is None:
                astro_start = crossing
        elif astro_end is None:
            astro_end = crossing
    return TwilightTimes(night=night, astro_start=astro_start, astro_end=astro_end)


# --- moon ------------------------------------------------------------------------------


@dataclass(frozen=True)
class MoonTrack:
    """The moon over the night at the site.

    - ``times`` — the obstime grid (astropy :class:`~astropy.time.Time`, UTC);
    - ``alt`` / ``az`` — the moon's altitude / azimuth on the grid
      (:class:`~astropy.units.Quantity`, degrees);
    - ``phase`` — the illuminated fraction on the grid
      (:class:`~astropy.units.Quantity`, dimensionless, ``[0, 1]``:
      0 = new moon, 1 = full moon), from the geocentric sun–moon elongation
      ``(1 - cos(elongation)) / 2``.
    """

    times: Time
    alt: u.Quantity
    az: u.Quantity
    phase: u.Quantity


def moon_track(
    site: Site | Mapping[str, Any],
    night: Any,
    *,
    step_minutes: float = DEFAULT_STEP_MINUTES,
) -> MoonTrack:
    """The moon's altitude/azimuth track and illuminated fraction over the
    night (astropy :func:`~astropy.coordinates.get_body` ``"moon"``/``"sun"``
    on the night grid, transformed to AltAz)."""
    site = _as_site(site)
    night = _as_night(night)
    times = night_grid(site, night, step_minutes)
    frame = AltAz(obstime=times, location=site.location)
    moon = get_body("moon", times, site.location)
    sun = get_body("sun", times, site.location)
    moon_altaz = moon.transform_to(frame)
    elongation = sun.separation(moon).to_value(u.rad)
    phase = (1.0 - np.cos(elongation)) / 2.0
    return MoonTrack(
        times=times,
        alt=moon_altaz.alt.to(u.deg),
        az=moon_altaz.az.to(u.deg),
        phase=phase * u.dimensionless_unscaled,
    )


# --- suggested queue order ---------------------------------------------------------------


def suggest_order(
    entries: Iterable[Any],
    site: Site | Mapping[str, Any],
    night: Any,
    *,
    step_minutes: float = DEFAULT_STEP_MINUTES,
) -> tuple[int, ...]:
    """The greedy suggested order of the queue entries, as a tuple of indices
    into ``entries``.

    Each target's best altitude window is centred on its culmination, so the
    greedy pass repeatedly takes the remaining target whose window peaks
    earliest (its ``transit_time``), breaking ties by the existing queue
    position — the result is stable w.r.t. the input order. Targets that
    never rise still culminate (possibly below the horizon) and are ordered
    by that time. Unresolved entries (no coordinate yet) cannot be scored
    and are deferred to the end, keeping their relative order.
    """
    site = _as_site(site)
    night = _as_night(night)
    entries = list(entries)
    resolved: list[tuple[int, Any]] = []
    deferred: list[int] = []
    for index, entry in enumerate(entries):
        if _try_entry_coord(entry) is None:
            deferred.append(index)
        else:
            resolved.append((index, entry))
    if not resolved:
        return tuple(deferred)
    curves = altitude_curves([entry for _, entry in resolved], site, night, step_minutes=step_minutes)
    remaining = [(curve.transit_time.jd, index) for (index, _), curve in zip(resolved, curves, strict=True)]
    order: list[int] = []
    while remaining:
        best = min(remaining)
        order.append(best[1])
        remaining.remove(best)
    return tuple(order + deferred)
