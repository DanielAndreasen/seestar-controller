"""Pure visibility-planner tests — no gi, no network (visibility-planner 01).

Covers the pure core of ``seestar_controller.visibility``:

- the package boundary: a subprocess test asserting that importing the
  package (and computing a full night with it) does NOT import ``gi`` and
  never touches the network (sockets are blocked in the subprocess), plus a
  source-scan audit of the package modules;
- :class:`~seestar_controller.visibility.Site` — validation, the astropy
  ``EarthLocation`` / tzinfo / local-time helpers, and the settings bridge
  :meth:`~...Site.from_settings` (key names asserted against
  ``ui.state.SETTINGS_KEYS``; round-trip through a real ``AppState``
  settings file);
- the night window / grid — local-noon-to-local-noon in the site timezone
  (incl. a DST-shift night), grid shape and validation;
- :func:`~seestar_controller.visibility.twilight_times` — fixed site/date vs
  an astropy reference (hardcoded reference values AND an independent
  10-second-grid recomputation, both within 1 minute), the sun altitude at
  the crossings, and the polar-day ``None`` case;
- :func:`~seestar_controller.visibility.altitude_curves` — per-target
  (times, alt, az) on the grid, the curve peaking at transit (vs an
  independent LST==RA bisection and a direct astropy AltAz recompute), and
  the unresolved-entry error;
- :func:`~seestar_controller.visibility.moon_track` — alt/az vs a direct
  ``get_body("moon")`` AltAz, phase vs the sun–moon elongation formula, and
  full/new-moon sanity nights;
- :func:`~seestar_controller.visibility.suggest_order` — orders by transit
  time, stable w.r.t. the existing queue order (identical targets keep
  their relative order), unresolved entries deferred to the end, works on a
  real ``PlanQueue``.

All astropy computations use the builtin ephemeris + bundled IERS-B (the
planner disables IERS auto-download), so nothing here touches the network.
"""

import datetime
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import astropy.units as u
import numpy as np
import pytest
from astropy.coordinates import AltAz, SkyCoord, get_body
from astropy.time import Time

from seestar_controller import visibility
from seestar_controller.catalog.coords import J2000Coord
from seestar_controller.observing.queue import PlanQueue, QueueEntry
from seestar_controller.ui.state import SETTINGS_KEYS as UI_SETTINGS_KEYS
from seestar_controller.ui.state import AppState
from seestar_controller.visibility import (
    ASTRO_TWILIGHT_ALT_DEG,
    DEFAULT_STEP_MINUTES,
    SITE_SETTINGS_KEYS,
    AltitudeCurve,
    MoonTrack,
    Site,
    TwilightTimes,
    VisibilityError,
    altitude_curves,
    moon_track,
    night_grid,
    night_window,
    suggest_order,
    twilight_times,
)

#: Directory of the visibility package (source-scan audit).
VISIBILITY_PACKAGE_DIR = Path(visibility.__file__).resolve().parent

#: The visibility package modules — all pure, no ``gi`` (subprocess-enforced).
VISIBILITY_MODULES = ("__init__", "site", "planner")

#: The fixed test site: Munich (48.1373 N, 11.5755 E, 520 m, Europe/Berlin).
MUNICH = Site(latitude_deg=48.1373, longitude_deg=11.5755, elevation_m=520.0, timezone="Europe/Berlin")

#: The fixed test night.
NIGHT = datetime.date(2025, 3, 20)

#: Astropy reference for the astronomical twilight of NIGHT at MUNICH
#: (astropy 8.0.3, builtin ephemeris + bundled IERS-B, sun crossing -18 deg):
#: evening dusk 2025-03-20 19:12:08 UTC, morning dawn 2025-03-21 03:29:01 UTC.
REF_ASTRO_START = Time("2025-03-20T19:12:08.042", scale="utc")
REF_ASTRO_END = Time("2025-03-21T03:29:00.558", scale="utc")

#: Test targets (J2000): ~M 42, and two mid-declination targets with clearly
#: separated transits on NIGHT at MUNICH.
COORD_M42 = J2000Coord(ra_deg=83.8215, dec_deg=-5.39114)
COORD_RA12 = J2000Coord.from_hours(12.0, 30.0)
COORD_RA20 = J2000Coord.from_hours(20.0, 20.0)
COORD_RA5 = J2000Coord.from_hours(5.0, 25.0)


def _has_tzdata() -> bool:
    try:
        ZoneInfo("Europe/Berlin")
    except Exception:  # noqa: BLE001
        return False
    return True


HAS_TZDATA = _has_tzdata()


def _entry(name, coord):
    return QueueEntry.resolved(name, coord)


# --- package boundary (subprocess + source scan) -----------------------------------------


def test_visibility_package_imports_no_gi_and_computes_offline():
    code = """
import socket

class _BlockedSocket:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("network access blocked in test")

def _blocked(*args, **kwargs):
    raise RuntimeError("network access blocked in test")

socket.socket = _BlockedSocket
socket.create_connection = _blocked
socket.getaddrinfo = _blocked

import sys
import datetime
import astropy.units as u
import seestar_controller.visibility as vis
import seestar_controller.visibility.site
import seestar_controller.visibility.planner

assert "gi" not in sys.modules, "GTK imported"
assert "seestar_controller.link" not in sys.modules, "link imported"
assert "seestar_controller.ui" not in sys.modules, "ui imported"

from seestar_controller.catalog.coords import J2000Coord

site = vis.Site(48.1373, 11.5755, 520.0, "Europe/Berlin")
night = datetime.date(2025, 3, 20)
tw = vis.twilight_times(site, night)
assert tw.astro_start is not None and tw.astro_end is not None
curves = vis.altitude_curves([J2000Coord(ra_deg=83.8215, dec_deg=-5.39114)], site, night)
assert len(curves) == 1 and curves[0].max_alt > 30 * u.deg
moon = vis.moon_track(site, night)
assert moon.alt.shape == moon.times.shape == moon.phase.shape
order = vis.suggest_order(
    [J2000Coord.from_hours(20.0, 20.0), J2000Coord.from_hours(5.0, 25.0)], site, night
)
assert order == (1, 0), order
print("clean")
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_every_visibility_module_is_pure_and_the_public_api_is_exported():
    modules = {path.stem for path in VISIBILITY_PACKAGE_DIR.glob("*.py")}
    assert modules == set(VISIBILITY_MODULES)
    for name in VISIBILITY_MODULES:
        source = (VISIBILITY_PACKAGE_DIR / f"{name}.py").read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), (
            f"visibility module {name} imports gi"
        )
        assert not re.search(
            r"^\s*(import subprocess|from subprocess)\b", source, re.MULTILINE
        ), f"visibility module {name} shells out"
        # the only in-repo imports allowed are catalog + visibility itself
        # (no link, no ui shell — the planner stays decoupled)
        for match in re.finditer(
            r"^\s*(?:from|import)\s+(seestar_controller[\w.]*)", source, re.MULTILINE
        ):
            top = match.group(1).split(".")[1]
            assert top in ("catalog", "visibility"), (
                f"visibility module {name} imports seestar_controller.{top}"
            )
    for name in visibility.__all__:
        assert hasattr(visibility, name), name


# --- Site ----------------------------------------------------------------------------------


def test_site_defaults_and_validation():
    site = Site(latitude_deg=48.1373, longitude_deg=11.5755)
    assert site.elevation_m == 0.0 and site.timezone == "UTC"
    assert isinstance(site, Site)
    for bad_kwargs in (
        {"latitude_deg": 90.0001},
        {"latitude_deg": -91},
        {"latitude_deg": "north"},
        {"latitude_deg": float("nan")},
        {"latitude_deg": True},
        {"longitude_deg": 180.5},
        {"longitude_deg": -180.5},
        {"elevation_m": 9001},
        {"elevation_m": -501},
        {"elevation_m": "high"},
        {"timezone": "Not/AZone"},
        {"timezone": ""},
        {"timezone": 42},
    ):
        kwargs = {"latitude_deg": 48.0, "longitude_deg": 11.0}
        kwargs.update(bad_kwargs)
        with pytest.raises(VisibilityError):
            Site(**kwargs)  # ty: ignore[invalid-argument-type]
    # VisibilityError is a ValueError
    with pytest.raises(ValueError):
        Site(latitude_deg=123.0, longitude_deg=0.0)


def test_site_location_and_tzinfo():
    loc = MUNICH.location
    assert loc.lat.to_value(u.deg) == pytest.approx(48.1373)
    assert loc.lon.to_value(u.deg) == pytest.approx(11.5755)
    assert loc.height.to_value(u.m) == pytest.approx(520.0)
    assert Site(0.0, 0.0).tzinfo() is datetime.UTC
    if HAS_TZDATA:
        assert MUNICH.tzinfo() == ZoneInfo("Europe/Berlin")


def test_site_local_time():
    t = Time("2025-03-20T19:12:08.042", scale="utc")
    local = MUNICH.local_time(t)
    assert local.utcoffset() == datetime.timedelta(hours=1)  # CET on Mar 20
    assert (local.year, local.month, local.day, local.hour, local.minute) == (
        2025,
        3,
        20,
        20,
        12,
    )
    # after the DST shift (Mar 30) the offset is +2
    local2 = MUNICH.local_time(Time("2025-03-31T19:12:08", scale="utc"))
    assert local2.utcoffset() == datetime.timedelta(hours=2)


def test_site_from_settings():
    settings = {
        "site_latitude": "48.1373",
        "site_longitude": "11.5755",
        "site_elevation_m": "520",
        "site_timezone": "UTC",
    }
    assert Site.from_settings(settings) == Site(48.1373, 11.5755, 520.0, "UTC")
    # elevation unset -> 0 m, timezone unset -> UTC, numbers accepted too
    assert Site.from_settings({"site_latitude": 48.1373, "site_longitude": 11.5755}) == Site(
        48.1373, 11.5755, 0.0, "UTC"
    )
    # latitude/longitude unset -> error (no sane default location)
    with pytest.raises(VisibilityError):
        Site.from_settings({})
    with pytest.raises(VisibilityError):
        Site.from_settings({"site_latitude": "48.0"})
    with pytest.raises(VisibilityError):
        Site.from_settings({"site_latitude": "48.0", "site_longitude": "abc"})
    with pytest.raises(VisibilityError):
        Site.from_settings({"site_latitude": "48.0", "site_longitude": "11.0", "site_elevation_m": "x"})
    with pytest.raises(VisibilityError):
        Site.from_settings("not a mapping")  # ty: ignore[invalid-argument-type]


def test_site_settings_key_names_match_ui_state():
    # the visibility package re-declares the settings key names (same pattern
    # as ui.state.DEFAULT_KEY_PATH) — they must match ui.state.SETTINGS_KEYS
    assert set(SITE_SETTINGS_KEYS) <= set(UI_SETTINGS_KEYS)


def test_site_from_app_state_settings_round_trip(tmp_path):
    settings_file = tmp_path / "config" / "settings.json"
    state = AppState(settings_file)
    with pytest.raises(VisibilityError):
        Site.from_settings(state.settings)  # fresh install: site unset
    state.set_setting("site_latitude", "48.1373")
    state.set_setting("site_longitude", "11.5755")
    state.set_setting("site_elevation_m", "520")
    state.set_setting("site_timezone", "UTC")
    state.save()
    loaded = AppState(settings_file).load()
    site = Site.from_settings(loaded.settings)
    assert site == Site(48.1373, 11.5755, 520.0, "UTC")
    # the planner functions accept the settings mapping directly as the site
    curves = altitude_curves([COORD_M42], loaded.settings, NIGHT)
    assert len(curves) == 1


# --- night window + grid ---------------------------------------------------------------------


def test_night_window_is_local_noon_to_local_noon():
    start, end = night_window(MUNICH, NIGHT)
    # 2025-03-20 12:00 Europe/Berlin (CET, UTC+1) = 11:00 UTC
    assert start == Time("2025-03-20T11:00:00", scale="utc")
    assert end == Time("2025-03-21T11:00:00", scale="utc")
    # DST shift night: 2025-03-30 12:00 CEST (UTC+2) = 10:00 UTC
    start, end = night_window(MUNICH, datetime.date(2025, 3, 29))
    assert start == Time("2025-03-29T11:00:00", scale="utc")
    assert end == Time("2025-03-30T10:00:00", scale="utc")


def test_night_window_accepts_settings_mapping_and_datetime():
    settings = {
        "site_latitude": "48.1373",
        "site_longitude": "11.5755",
        "site_elevation_m": "520",
        "site_timezone": "UTC",
    }
    start, end = night_window(settings, datetime.datetime(2025, 3, 20, 23, 30, tzinfo=datetime.UTC))
    assert start == Time("2025-03-20T12:00:00", scale="utc")
    assert end == Time("2025-03-21T12:00:00", scale="utc")


def test_night_grid_shape_and_validation():
    grid = night_grid(MUNICH, NIGHT)
    start, end = night_window(MUNICH, NIGHT)
    assert len(grid) == int(24 * 60 / DEFAULT_STEP_MINUTES) + 1 == 289
    assert grid[0] == start and grid[-1] == end
    assert np.allclose(np.diff((grid - grid[0]).to_value(u.min)), DEFAULT_STEP_MINUTES)
    fine = night_grid(MUNICH, NIGHT, step_minutes=1.0)
    assert len(fine) == 24 * 60 + 1
    for bad in (0, -5, 24 * 60 + 1, float("nan"), "5", None, True):
        with pytest.raises(VisibilityError):
            night_grid(MUNICH, NIGHT, step_minutes=bad)  # ty: ignore[invalid-argument-type]
    with pytest.raises(VisibilityError):
        night_grid(MUNICH, "2025-03-20")
    with pytest.raises(VisibilityError):
        night_window(MUNICH, None)
    with pytest.raises(VisibilityError):
        night_grid("not a site", NIGHT)  # ty: ignore[invalid-argument-type]


# --- twilight times ----------------------------------------------------------------------------


def _reference_twilight_crossings(site, night, sample_s=10.0):
    """Independent astropy reference: sample the sun altitude on a dense grid
    and linearly interpolate the -18 deg crossings (a different method than
    the module's 1-minute grid + bisection)."""
    start, end = night_window(site, night)
    total_s = (end - start).to_value(u.s)
    grid = start + np.arange(0.0, total_s + sample_s, sample_s) * u.s
    frame = AltAz(obstime=grid, location=site.location)
    sun_alt = get_body("sun", grid, site.location).transform_to(frame).alt.to_value(u.deg)
    f = sun_alt - ASTRO_TWILIGHT_ALT_DEG
    crossings = []
    for i in range(len(grid) - 1):
        if f[i] * f[i + 1] < 0:
            frac = -f[i] / (f[i + 1] - f[i])
            crossings.append(grid[i] + frac * (grid[i + 1] - grid[i]))
    return crossings


def test_twilight_times_match_astropy_reference_within_a_minute():
    tw = twilight_times(MUNICH, NIGHT)
    assert isinstance(tw, TwilightTimes)
    assert tw.night == NIGHT
    assert tw.astro_start is not None and tw.astro_end is not None
    # hardcoded astropy reference values (see REF_* above)
    assert abs((tw.astro_start - REF_ASTRO_START).to_value(u.s)) < 60.0
    assert abs((tw.astro_end - REF_ASTRO_END).to_value(u.s)) < 60.0
    # independent dense-grid recomputation
    ref_start, ref_end = _reference_twilight_crossings(MUNICH, NIGHT)
    assert abs((tw.astro_start - ref_start).to_value(u.s)) < 60.0
    assert abs((tw.astro_end - ref_end).to_value(u.s)) < 60.0
    # ordering + containment in the night window
    start, end = night_window(MUNICH, NIGHT)
    assert start < tw.astro_start < tw.astro_end < end
    # the sun is at -18 deg at both crossings
    for t in (tw.astro_start, tw.astro_end):
        frame = AltAz(obstime=t, location=MUNICH.location)
        alt = get_body("sun", t, MUNICH.location).transform_to(frame).alt.to_value(u.deg)
        assert alt == pytest.approx(ASTRO_TWILIGHT_ALT_DEG, abs=1e-6)
    # local rendering: evening dusk ~20:12 CET, morning dawn ~04:29 CET
    assert MUNICH.local_time(tw.astro_start).hour == 20
    assert MUNICH.local_time(tw.astro_end).hour == 4


def test_twilight_times_polar_day_are_none():
    tromso = Site(69.6492, 18.9553, 0.0, "Europe/Oslo")
    # midnight sun: the sun never reaches -18 deg -> no astronomical twilight
    tw = twilight_times(tromso, datetime.date(2025, 6, 15))
    assert tw.astro_start is None and tw.astro_end is None


def test_twilight_times_accept_settings_mapping():
    settings = {
        "site_latitude": "48.1373",
        "site_longitude": "11.5755",
        "site_elevation_m": "520",
        "site_timezone": "UTC",
    }
    tw = twilight_times(settings, NIGHT)
    assert tw.astro_start is not None
    assert abs((tw.astro_start - REF_ASTRO_START).to_value(u.s)) < 60.0 + 3600.0  # UTC site


# --- altitude curves ----------------------------------------------------------------------------


def _lst_ra_crossing(site, night, ra_deg, step_minutes=DEFAULT_STEP_MINUTES):
    """Independent transit reference: bisect the apparent LST == RA crossing."""
    grid = night_grid(site, night, step_minutes)
    lon = site.location.lon

    def offset(t):
        lst = t.sidereal_time("apparent", longitude=lon).to_value(u.deg)
        return (lst - ra_deg + 180.0) % 360.0 - 180.0

    d = np.array([offset(t) for t in grid])
    for i in range(len(grid) - 1):
        # upper culmination = LST crossing RA upwards (d goes - -> +);
        # the downward crossing is the lower culmination (anti-transit)
        if d[i] < 0.0 <= d[i + 1]:
            lo, hi, f_lo = grid[i], grid[i + 1], d[i]
            for _ in range(60):
                mid = lo + (hi - lo) / 2
                f_mid = offset(mid)
                if (f_lo < 0.0) == (f_mid < 0.0):
                    lo, f_lo = mid, f_mid
                else:
                    hi = mid
            return lo + (hi - lo) / 2
    return None


def test_altitude_curves_shape_and_values():
    entries = [
        _entry("M 42", COORD_M42),
        QueueEntry(coord=COORD_RA12),
        COORD_RA20,  # a plain J2000Coord works too
    ]
    curves = altitude_curves(entries, MUNICH, NIGHT)
    assert len(curves) == 3
    assert all(isinstance(c, AltitudeCurve) for c in curves)
    assert [c.name for c in curves] == ["M 42", str(COORD_RA12), str(COORD_RA20)]
    grid = night_grid(MUNICH, NIGHT)
    for curve in curves:
        assert np.all(curve.times.jd == grid.jd)
        assert curve.alt.unit == u.deg and curve.az.unit == u.deg
        assert curve.alt.shape == curve.az.shape == grid.shape
        assert np.all(curve.az.to_value(u.deg) >= 0.0)
        assert np.all(curve.az.to_value(u.deg) < 360.0)
        assert np.all(curve.alt.to_value(u.deg) <= 90.0)
        assert np.all(curve.alt.to_value(u.deg) >= -90.0)
        # alt/az match a direct astropy AltAz computation on the same grid
        coord = {"M 42": COORD_M42, str(COORD_RA12): COORD_RA12, str(COORD_RA20): COORD_RA20}[
            curve.name
        ]
        sky = SkyCoord(ra=coord.ra_deg * u.deg, dec=coord.dec_deg * u.deg, frame="icrs")
        altaz = sky.transform_to(AltAz(obstime=grid, location=MUNICH.location))
        assert np.allclose(curve.alt.to_value(u.deg), altaz.alt.to_value(u.deg), atol=1e-9)
        assert np.allclose(curve.az.to_value(u.deg), altaz.az.to_value(u.deg), atol=1e-9)


def test_altitude_curve_peaks_at_transit():
    entries = [_entry("M 42", COORD_M42), QueueEntry(coord=COORD_RA12), QueueEntry(coord=COORD_RA20)]
    curves = altitude_curves(entries, MUNICH, NIGHT)
    step_s = DEFAULT_STEP_MINUTES * 60.0
    for entry, curve in zip(entries, curves):
        coord = entry.coord
        assert isinstance(coord, J2000Coord)
        # the curve's peak is the argmax of alt and is stored as transit_time
        peak = int(np.argmax(curve.alt.to_value(u.deg)))
        assert curve.transit_time == curve.times[peak]
        assert curve.max_alt == curve.alt[peak]
        # ... and it coincides with the LST == RA crossing (within one grid step)
        crossing = _lst_ra_crossing(MUNICH, NIGHT, coord.ra_deg)
        assert crossing is not None
        assert abs((curve.transit_time - crossing).to_value(u.s)) <= step_s + 1.0
        # ... and with a direct astropy AltAz altitude at that instant
        sky = SkyCoord(ra=coord.ra_deg * u.deg, dec=coord.dec_deg * u.deg, frame="icrs")
        alt = sky.transform_to(AltAz(obstime=curve.transit_time, location=MUNICH.location)).alt
        assert curve.max_alt.to_value(u.deg) == pytest.approx(alt.to_value(u.deg), abs=1e-9)


def test_altitude_curves_reject_unresolved_entries():
    with pytest.raises(VisibilityError):
        altitude_curves([QueueEntry(name="M 42")], MUNICH, NIGHT)  # unresolved name
    with pytest.raises(VisibilityError):
        # a duck-typed entry whose coord is not a J2000Coord
        altitude_curves([SimpleNamespace(name="X", coord="not a coord")], MUNICH, NIGHT)
    assert altitude_curves([], MUNICH, NIGHT) == ()


# --- moon track -----------------------------------------------------------------------------------


def test_moon_track_matches_astropy_reference():
    track = moon_track(MUNICH, NIGHT)
    assert isinstance(track, MoonTrack)
    grid = night_grid(MUNICH, NIGHT)
    assert np.all(track.times.jd == grid.jd)
    frame = AltAz(obstime=grid, location=MUNICH.location)
    moon = get_body("moon", grid, MUNICH.location)
    sun = get_body("sun", grid, MUNICH.location)
    moon_altaz = moon.transform_to(frame)
    assert np.allclose(track.alt.to_value(u.deg), moon_altaz.alt.to_value(u.deg), atol=1e-9)
    assert np.allclose(track.az.to_value(u.deg), moon_altaz.az.to_value(u.deg), atol=1e-9)
    # phase = (1 - cos(sun-moon elongation)) / 2, in [0, 1]
    elong = sun.separation(moon).to_value(u.rad)
    phase = (1.0 - np.cos(elong)) / 2.0
    assert np.allclose(track.phase.to_value(u.dimensionless_unscaled), phase, atol=1e-12)
    assert np.all(track.phase.to_value(u.dimensionless_unscaled) >= 0.0)
    assert np.all(track.phase.to_value(u.dimensionless_unscaled) <= 1.0)


def test_moon_track_full_and_new_moon_nights():
    # full moon 2025-03-14: illuminated fraction ~1 all night
    full = moon_track(MUNICH, datetime.date(2025, 3, 14))
    assert full.phase.to_value(u.dimensionless_unscaled).max() > 0.99
    # new moon 2025-03-29: illuminated fraction ~0 all night
    new = moon_track(MUNICH, datetime.date(2025, 3, 29))
    assert new.phase.to_value(u.dimensionless_unscaled).max() < 0.05


# --- suggested order -------------------------------------------------------------------------------


def test_suggest_order_orders_by_transit_time():
    # input order is deliberately NOT the transit order on NIGHT at MUNICH:
    # RA 20h transits last (07:20 UTC next day), RA 5h first (16:55 UTC)
    entries = [QueueEntry(coord=COORD_RA20), QueueEntry(coord=COORD_RA12), QueueEntry(coord=COORD_RA5)]
    order = suggest_order(entries, MUNICH, NIGHT)
    assert order == (2, 1, 0)
    # sanity: the transits really are in that order
    curves = altitude_curves(entries, MUNICH, NIGHT)
    transits = [c.transit_time.jd for c in curves]
    assert transits[2] < transits[1] < transits[0]


def test_suggest_order_is_stable_wrt_existing_order():
    # two entries with the SAME coordinate (same transit) keep their relative
    # input order; the earlier-transiting target goes first
    entries = [
        QueueEntry(coord=COORD_RA12),  # transit 23:20 UTC
        QueueEntry(coord=COORD_RA5),  # transit 16:55 UTC -> first
        QueueEntry(coord=COORD_RA5),  # same transit as index 1 -> after it
    ]
    assert suggest_order(entries, MUNICH, NIGHT) == (1, 2, 0)


def test_suggest_order_defers_unresolved_entries_to_the_end():
    entries = [
        QueueEntry(name="Unresolved"),
        QueueEntry(coord=COORD_RA20),
        QueueEntry(coord=COORD_RA5),
        QueueEntry(name="Also Unresolved"),
    ]
    order = suggest_order(entries, MUNICH, NIGHT)
    assert order == (2, 1, 0, 3)  # resolved by transit, unresolved last in order
    # all unresolved -> the original order
    assert suggest_order([QueueEntry(name="a"), QueueEntry(name="b")], MUNICH, NIGHT) == (0, 1)
    # empty queue
    assert suggest_order([], MUNICH, NIGHT) == ()


def test_suggest_order_works_on_a_plan_queue_and_is_a_permutation():
    queue = PlanQueue(
        [
            QueueEntry(coord=COORD_RA20),
            _entry("M 42", COORD_M42),
            QueueEntry(coord=COORD_RA12),
        ]
    )
    order = suggest_order(queue, MUNICH, NIGHT)
    assert sorted(order) == [0, 1, 2]
    # M 42 (RA 5h35m) transits before RA 12h, which transits before RA 20h
    assert order == (1, 2, 0)
