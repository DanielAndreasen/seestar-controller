# 01: Visibility planner core (pure module)

**What to build:** the pure (no-gi) core of the visibility planner: given the observing PlanQueue entries (resolved ra/dec), the observing site (lat/lon/elevation) and timezone from settings, and the night date, compute per-target altitude/azimuth curves over the night (astropy AltAz on an obstime grid), astronomical twilight start/end, and moon altitude/phase (astropy.coordinates.get_body), plus a greedy suggested queue order by best altitude window (existing order as tiebreak). Demoable: known site/date -> twilight times match astropy reference within a minute, altitude curve peaks at transit, suggestion orders by transit; site coords persisted in settings.

**Blocked by:** None (observing queue, catalog coords, ui-shell all done). Extend ui/state.py settings with site lat/lon/elevation + timezone, persisted in settings.json.

**Status:** ready-for-agent

- [x] Pure `visibility/` (or `observing/planner.py`) package, no gi (subprocess boundary test); astropy.coordinates AltAz + get_body("sun"/"moon")
- [x] `altitude_curves(entries, site, night)` -> per-target (times, alt, az); `twilight_times(site, night)`; `moon_track(site, night)` (alt + phase)
- [x] `suggest_order(entries, site, night)` greedy by altitude window, stable w.r.t. existing order
- [x] Settings: site coords + timezone in ui/state.py, saved to settings.json, editable in a settings dialog (or accept settings-file only for this ticket — decide with existing patterns)
- [x] Tests: fixed site/date vs astropy reference (twilight within 1 min, transit peak, ordering); settings round-trip; no-gi boundary; no network
- [x] Full suite green

## Comments

Implemented as a new pure package `src/seestar_controller/visibility/` (`__init__.py` re-exports, `site.py`, `planner.py`) — stdlib + numpy + astropy + `seestar_controller.catalog` only, no `gi`, no subprocess. A subprocess test imports the package with sockets blocked and asserts no `gi`/link/ui module lands in `sys.modules`; a source-scan test audits the import lines. `iers.conf.auto_download = False` is set at import so astropy never touches the network (bundled IERS-B + builtin ephemeris).

- `Site` (frozen dataclass: latitude_deg/longitude_deg/elevation_m/timezone) with validation, `EarthLocation`/`tzinfo()`/`local_time()` helpers, and `Site.from_settings()` bridging the settings dict (key names re-declared in the package, asserted against `ui.state.SETTINGS_KEYS`).
- Night window = local noon → local noon+1 day at the site timezone (DST-safe wall-clock arithmetic), converted to UTC; `night_grid()` samples it (default 5 min). `twilight_times()` = sun crossing −18° on a 1-min grid + bisection (`None` for polar day/night). `altitude_curves()` = AltAz on the grid per resolved entry (raises `VisibilityError` on unresolved entries). `moon_track()` = moon AltAz + phase from the sun–moon elongation. `suggest_order()` = greedy by transit time with the original queue index as tiebreak (stable; unresolved entries deferred to the end).
- Settings: `site_latitude` / `site_longitude` / `site_elevation_m` / `site_timezone` added to `SETTINGS_KEYS`/`DEFAULT_SETTINGS` in `ui/state.py` with public validators, persisted in `settings.json`. Settings-file only for this ticket (not in `EDITABLE_SETTINGS_KEYS`); the settings-dialog UI is deferred to the follow-up ticket that owns the dialog.
- Tests (`tests/test_visibility_planner.py`, 23 tests): Munich 2025-03-20 vs hardcoded astropy reference twilight (within 1 min) AND an independent 10-s-grid recomputation; DST night window; polar-day `None`; altitude curve peaks at transit vs an independent LST==RA bisection and a direct AltAz recompute; moon phase full/new sanity; suggestion ordering + stability; settings round-trip through a real `AppState`; no-gi/no-network subprocess boundary. Full suite: 1787 passed, 5 skipped (gi widgets). `ty check` and `ruff check` clean.
