"""The observing site for the visibility planner — pure Python, no ``gi``.

This is part of the PURE layer of :mod:`seestar_controller.visibility` (the
visibility-planner spec, ``.scratch/visibility-planner/spec.md``): **no
``gi`` import anywhere in this package** — stdlib + :mod:`numpy` +
:mod:`astropy` + :mod:`seestar_controller.catalog` only — so it is fully
unit-testable headless (the sandbox has no GTK, see docs/wiki/assumptions.md;
a subprocess test enforces the boundary).

What lives here:

- :class:`VisibilityError` — the package's error type (a :class:`ValueError`)
  for an invalid site, night, or queue entry;
- :class:`Site` — the observing site: latitude/longitude in degrees,
  elevation in metres, and an IANA timezone name (the timezone the *night
  date* is expressed in — the planner builds the night window in local time
  and converts to UTC for astropy). Validation happens in ``__post_init__``
  and raises :class:`VisibilityError`. Helpers: :meth:`Site.location` (the
  :class:`~astropy.coordinates.EarthLocation`), :meth:`Site.tzinfo` (the
  :class:`~zoneinfo.ZoneInfo`), :meth:`Site.local_time` (an astropy
  :class:`~astropy.time.Time` rendered as an aware local ``datetime``);
- :meth:`Site.from_settings` — the bridge from the ui settings dict (the
  ``site_latitude`` / ``site_longitude`` / ``site_elevation_m`` /
  ``site_timezone`` keys persisted in ``settings.json`` by
  :mod:`seestar_controller.ui.state`) to a :class:`Site`. The key names are
  re-declared here (the same re-declaration pattern the repo uses for
  cross-package constants, e.g. ``ui.state.DEFAULT_KEY_PATH``) so this
  package does not import the ui shell; a test asserts the names match
  :data:`seestar_controller.ui.state.SETTINGS_KEYS`. Latitude/longitude
  unset (``""``) raises :class:`VisibilityError` — there is no sane default
  location; elevation unset means 0 m and timezone unset means UTC.

The site is where the night is anchored: the planner's night window runs from
local noon of the night date to local noon of the next day (see
:mod:`.planner`), so both the evening and the morning astronomical twilight
of that night fall inside the window at any latitude.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import timezone as dt_timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import astropy.units as u
from astropy.coordinates import EarthLocation
from astropy.time import Time

#: Settings key holding the site latitude in degrees (mirrored from
#: :mod:`seestar_controller.ui.state` — see module docstring).
SITE_LATITUDE_KEY = "site_latitude"
#: Settings key holding the site longitude in degrees (mirrored).
SITE_LONGITUDE_KEY = "site_longitude"
#: Settings key holding the site elevation in metres (mirrored).
SITE_ELEVATION_KEY = "site_elevation_m"
#: Settings key holding the site IANA timezone name (mirrored).
SITE_TIMEZONE_KEY = "site_timezone"

#: All site settings keys, in :class:`Site` field order.
SITE_SETTINGS_KEYS: tuple[str, ...] = (
    SITE_LATITUDE_KEY,
    SITE_LONGITUDE_KEY,
    SITE_ELEVATION_KEY,
    SITE_TIMEZONE_KEY,
)

#: Latitude bounds, degrees.
SITE_LATITUDE_MIN_DEG = -90.0
SITE_LATITUDE_MAX_DEG = 90.0
#: Longitude bounds, degrees.
SITE_LONGITUDE_MIN_DEG = -180.0
SITE_LONGITUDE_MAX_DEG = 180.0
#: Elevation bounds, metres — below the Dead Sea shore to above Mt Everest.
SITE_ELEVATION_MIN_M = -500.0
SITE_ELEVATION_MAX_M = 9000.0

#: The timezone used when the setting is unset: UTC.
DEFAULT_SITE_TIMEZONE = "UTC"


class VisibilityError(ValueError):
    """A visibility-planner input error — a :class:`ValueError` with a clear
    message (invalid site field, invalid night, unresolved queue entry, ...)."""


def _check_timezone_name(name: str) -> None:
    """Validate an IANA timezone name; raises :class:`VisibilityError`.

    ``"UTC"`` is always accepted (it maps to :class:`datetime.timezone.utc`,
    which needs no tzdata); any other name must resolve through
    :class:`zoneinfo.ZoneInfo` on this machine.
    """
    if name == "UTC":
        return
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError) as exc:
        raise VisibilityError(
            f"timezone {name!r} is not a valid IANA timezone name ({exc})"
        ) from exc


@dataclass(frozen=True)
class Site:
    """The observing site: where the night is observed from.

    - ``latitude_deg`` — latitude in degrees, ``[-90, 90]`` (north positive);
    - ``longitude_deg`` — longitude in degrees, ``[-180, 180]`` (east
      positive, so e.g. Munich is ``+11.58`` and New York is ``-74.0``);
    - ``elevation_m`` — elevation above the reference ellipsoid in metres,
      ``[-500, 9000]`` (default 0);
    - ``timezone`` — the IANA timezone name the night date is expressed in
      (default ``"UTC"``), e.g. ``"Europe/Berlin"``.

    All fields are validated in ``__post_init__``; violations raise
    :class:`VisibilityError`.
    """

    latitude_deg: float
    longitude_deg: float
    elevation_m: float = 0.0
    timezone: str = DEFAULT_SITE_TIMEZONE

    def __post_init__(self) -> None:
        for name in ("latitude_deg", "longitude_deg", "elevation_m"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise VisibilityError(f"{name} must be a number, got {value!r}")
            if not math.isfinite(float(value)):
                raise VisibilityError(f"{name} must be finite, got {value!r}")
        lat = float(self.latitude_deg)
        if not SITE_LATITUDE_MIN_DEG <= lat <= SITE_LATITUDE_MAX_DEG:
            raise VisibilityError(
                f"latitude_deg must be in "
                f"[{SITE_LATITUDE_MIN_DEG}, {SITE_LATITUDE_MAX_DEG}] deg, got {lat!r}"
            )
        lon = float(self.longitude_deg)
        if not SITE_LONGITUDE_MIN_DEG <= lon <= SITE_LONGITUDE_MAX_DEG:
            raise VisibilityError(
                f"longitude_deg must be in "
                f"[{SITE_LONGITUDE_MIN_DEG}, {SITE_LONGITUDE_MAX_DEG}] deg, got {lon!r}"
            )
        elev = float(self.elevation_m)
        if not SITE_ELEVATION_MIN_M <= elev <= SITE_ELEVATION_MAX_M:
            raise VisibilityError(
                f"elevation_m must be in "
                f"[{SITE_ELEVATION_MIN_M}, {SITE_ELEVATION_MAX_M}] m, got {elev!r}"
            )
        if not isinstance(self.timezone, str) or not self.timezone.strip():
            raise VisibilityError(f"timezone must be a non-empty string, got {self.timezone!r}")
        tz = self.timezone.strip()
        _check_timezone_name(tz)
        object.__setattr__(self, "latitude_deg", lat)
        object.__setattr__(self, "longitude_deg", lon)
        object.__setattr__(self, "elevation_m", elev)
        object.__setattr__(self, "timezone", tz)

    @property
    def location(self) -> EarthLocation:
        """The site as an :class:`~astropy.coordinates.EarthLocation`."""
        return EarthLocation(
            lat=self.latitude_deg * u.deg,
            lon=self.longitude_deg * u.deg,
            height=self.elevation_m * u.m,
        )

    def tzinfo(self) -> ZoneInfo | dt_timezone:
        """The site timezone as a tzinfo (``"UTC"`` maps to
        :class:`datetime.timezone.utc`, which needs no tzdata)."""
        if self.timezone == "UTC":
            return UTC
        return ZoneInfo(self.timezone)

    def local_time(self, time: Time) -> datetime:
        """An astropy :class:`~astropy.time.Time` as an aware local
        ``datetime`` at the site (for display: the night date and the
        twilight times are local civil times)."""
        # to_datetime(timezone=...) attaches the tz explicitly — a naive
        # astimezone() would assume the *system* timezone instead
        return time.utc.to_datetime(timezone=self.tzinfo())

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> Site:
        """Build a :class:`Site` from a settings mapping (e.g.
        :meth:`seestar_controller.ui.state.AppState.settings`).

        Reads the ``site_latitude`` / ``site_longitude`` / ``site_elevation_m``
        / ``site_timezone`` keys (see :data:`SITE_SETTINGS_KEYS`). Missing
        keys fall back to unset (``""``): latitude/longitude unset raises
        :class:`VisibilityError` (no sane default location), elevation unset
        means 0 m, timezone unset means UTC. Values may be strings (the
        settings dict stores strings) or numbers; invalid values raise
        :class:`VisibilityError`.
        """
        if not isinstance(settings, Mapping):
            raise VisibilityError(f"settings must be a mapping, got {settings!r}")

        def _raw(key: str) -> Any:
            value = settings.get(key, "")
            return "" if value is None else value

        lat_raw = _raw(SITE_LATITUDE_KEY)
        lon_raw = _raw(SITE_LONGITUDE_KEY)
        if isinstance(lat_raw, str) and not lat_raw.strip():
            raise VisibilityError(
                f"site latitude is not set ({SITE_LATITUDE_KEY!r} is empty) — "
                "set the observing site in settings.json"
            )
        if isinstance(lon_raw, str) and not lon_raw.strip():
            raise VisibilityError(
                f"site longitude is not set ({SITE_LONGITUDE_KEY!r} is empty) — "
                "set the observing site in settings.json"
            )
        try:
            latitude = float(lat_raw)
            longitude = float(lon_raw)
        except (TypeError, ValueError):
            raise VisibilityError(
                f"site latitude/longitude must be numbers, got "
                f"{lat_raw!r} / {lon_raw!r}"
            ) from None
        elev_raw = _raw(SITE_ELEVATION_KEY)
        try:
            elevation = (
                0.0 if isinstance(elev_raw, str) and not elev_raw.strip() else float(elev_raw)
            )
        except (TypeError, ValueError):
            raise VisibilityError(
                f"site elevation must be a number of metres, got {elev_raw!r}"
            ) from None
        tz_raw = _raw(SITE_TIMEZONE_KEY)
        timezone_name = (
            DEFAULT_SITE_TIMEZONE if isinstance(tz_raw, str) and not tz_raw.strip() else str(tz_raw)
        )
        return cls(
            latitude_deg=latitude,
            longitude_deg=longitude,
            elevation_m=elevation,
            timezone=timezone_name,
        )

    def __str__(self) -> str:
        return (
            f"site {self.latitude_deg:+.4f}° {self.longitude_deg:+.4f}° "
            f"{self.elevation_m:g} m ({self.timezone})"
        )
