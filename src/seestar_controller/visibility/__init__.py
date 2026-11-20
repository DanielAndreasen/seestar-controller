"""Visibility planner — the pure (no-gi) core: curves, twilight, moon, order.

Spec: ``.scratch/visibility-planner/spec.md``. This package is the PURE layer
of the visibility planner: given the observing site (lat/lon/elevation +
timezone, from the ui settings), the night date, and the resolved plan-queue
entries, it computes per-target altitude/azimuth curves over the night
(astropy :class:`~astropy.coordinates.AltAz` on an obstime grid), the
astronomical twilight start/end, the moon track (altitude + illuminated
fraction, astropy :func:`~astropy.coordinates.get_body`), and a greedy
suggested queue order by best altitude window (existing order as tiebreak).

**No ``gi`` import anywhere in this package** — stdlib + :mod:`numpy` +
:mod:`astropy` + :mod:`seestar_controller.catalog` only — fully unit-testable
headless (the sandbox has no GTK, see docs/wiki/assumptions.md; a subprocess
test enforces the boundary). No network either: builtin ephemeris + bundled
IERS-B (see :mod:`.planner`). The timeline view that renders these results is
ticket 02 and lives in the widget layer.

Modules:

- :mod:`.site` — :class:`~.site.Site` (the observing site) +
  :class:`~.site.VisibilityError` + the settings bridge
  :meth:`~.site.Site.from_settings`;
- :mod:`.planner` — :func:`~.planner.night_window` / :func:`~.planner.night_grid`,
  :func:`~.planner.altitude_curves`, :func:`~.planner.twilight_times`,
  :func:`~.planner.moon_track`, :func:`~.planner.suggest_order` and the
  result types :class:`~.planner.AltitudeCurve` / :class:`~.planner.TwilightTimes`
  / :class:`~.planner.MoonTrack`.
"""

from .planner import (
    ASTRO_TWILIGHT_ALT_DEG,
    DEFAULT_STEP_MINUTES,
    NIGHT_WINDOW_HOURS,
    AltitudeCurve,
    MoonTrack,
    TwilightTimes,
    altitude_curves,
    moon_track,
    night_grid,
    night_window,
    suggest_order,
    twilight_times,
)
from .site import (
    DEFAULT_SITE_TIMEZONE,
    SITE_ELEVATION_KEY,
    SITE_ELEVATION_MAX_M,
    SITE_ELEVATION_MIN_M,
    SITE_LATITUDE_KEY,
    SITE_LATITUDE_MAX_DEG,
    SITE_LATITUDE_MIN_DEG,
    SITE_LONGITUDE_KEY,
    SITE_LONGITUDE_MAX_DEG,
    SITE_LONGITUDE_MIN_DEG,
    SITE_SETTINGS_KEYS,
    SITE_TIMEZONE_KEY,
    Site,
    VisibilityError,
)

__all__ = [
    "ASTRO_TWILIGHT_ALT_DEG",
    "DEFAULT_SITE_TIMEZONE",
    "DEFAULT_STEP_MINUTES",
    "NIGHT_WINDOW_HOURS",
    "SITE_ELEVATION_KEY",
    "SITE_ELEVATION_MAX_M",
    "SITE_ELEVATION_MIN_M",
    "SITE_LATITUDE_KEY",
    "SITE_LATITUDE_MAX_DEG",
    "SITE_LATITUDE_MIN_DEG",
    "SITE_LONGITUDE_KEY",
    "SITE_LONGITUDE_MAX_DEG",
    "SITE_LONGITUDE_MIN_DEG",
    "SITE_SETTINGS_KEYS",
    "SITE_TIMEZONE_KEY",
    "AltitudeCurve",
    "MoonTrack",
    "Site",
    "TwilightTimes",
    "VisibilityError",
    "altitude_curves",
    "moon_track",
    "night_grid",
    "night_window",
    "suggest_order",
    "twilight_times",
]
