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
  / :class:`~.planner.MoonTrack`;
- :mod:`.timeline` (ticket 02) — the night-timeline model behind the timeline
  view: :class:`~.timeline.TimelineModel` + :func:`~.timeline.build_timeline`,
  :func:`~.timeline.reorder_moves` / :func:`~.timeline.apply_order` (write a
  suggested order back to the plan queue through its public ``move`` API),
  :func:`~.timeline.suggested_labels` and the display helpers
  :func:`~.timeline.axis_hours` / :func:`~.timeline.format_local`;
- :mod:`.render` (ticket 02) — :func:`~.render.render_timeline_png`: the model
  rendered to PNG bytes with matplotlib's Agg backend (no pyplot, no display).

**Widget layer** (ticket 02): :mod:`.timeline_view` —
:class:`~.timeline_view.VisibilityView`, the night-timeline view (plot +
suggested order + Apply + Export PNG). It imports ``gi`` at module top and is
re-exported here **lazily** (PEP 562 ``__getattr__``), so
``import seestar_controller.visibility`` never imports ``gi``.
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
from .render import (
    DEFAULT_DPI,
    DEFAULT_HEIGHT_PX,
    DEFAULT_WIDTH_PX,
    HORIZON_COLOR,
    MAX_RENDER_PX,
    MOON_COLOR,
    TIMELINE_PALETTE,
    TWILIGHT_COLOR,
    X_TICK_STEP_HOURS,
    render_timeline_png,
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
from .timeline import (
    MAX_TIMELINE_ENTRIES,
    SettingsSource,
    TimelineModel,
    apply_order,
    axis_hours,
    build_timeline,
    format_local,
    reorder_moves,
    resolve_settings,
    suggested_labels,
)

#: Widget-layer names re-exported lazily (importing the module pulls in
#: ``gi`` — only the GUI path does).
_WIDGET_EXPORTS = {
    "VisibilityView": "timeline_view",
}


def __getattr__(name: str):
    if name in _WIDGET_EXPORTS:
        from importlib import import_module

        module = import_module(f".{_WIDGET_EXPORTS[name]}", __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ASTRO_TWILIGHT_ALT_DEG",
    "DEFAULT_DPI",
    "DEFAULT_HEIGHT_PX",
    "DEFAULT_SITE_TIMEZONE",
    "DEFAULT_STEP_MINUTES",
    "DEFAULT_WIDTH_PX",
    "HORIZON_COLOR",
    "MAX_RENDER_PX",
    "MAX_TIMELINE_ENTRIES",
    "MOON_COLOR",
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
    "TIMELINE_PALETTE",
    "TWILIGHT_COLOR",
    "X_TICK_STEP_HOURS",
    "AltitudeCurve",
    "MoonTrack",
    "SettingsSource",
    "Site",
    "TimelineModel",
    "TwilightTimes",
    "VisibilityError",
    # widget layer (lazy, imports gi):
    "VisibilityView",
    "altitude_curves",
    "apply_order",
    "axis_hours",
    "build_timeline",
    "format_local",
    "moon_track",
    "night_grid",
    "night_window",
    "render_timeline_png",
    "reorder_moves",
    "resolve_settings",
    "suggest_order",
    "suggested_labels",
    "twilight_times",
]
