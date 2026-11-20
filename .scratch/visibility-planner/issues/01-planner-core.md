# 01: Visibility planner core (pure module)

**What to build:** the pure (no-gi) core of the visibility planner: given the observing PlanQueue entries (resolved ra/dec), the observing site (lat/lon/elevation) and timezone from settings, and the night date, compute per-target altitude/azimuth curves over the night (astropy AltAz on an obstime grid), astronomical twilight start/end, and moon altitude/phase (astropy.coordinates.get_body), plus a greedy suggested queue order by best altitude window (existing order as tiebreak). Demoable: known site/date -> twilight times match astropy reference within a minute, altitude curve peaks at transit, suggestion orders by transit; site coords persisted in settings.

**Blocked by:** None (observing queue, catalog coords, ui-shell all done). Extend ui/state.py settings with site lat/lon/elevation + timezone, persisted in settings.json.

**Status:** ready-for-agent

- [ ] Pure `visibility/` (or `observing/planner.py`) package, no gi (subprocess boundary test); astropy.coordinates AltAz + get_body("sun"/"moon")
- [ ] `altitude_curves(entries, site, night)` -> per-target (times, alt, az); `twilight_times(site, night)`; `moon_track(site, night)` (alt + phase)
- [ ] `suggest_order(entries, site, night)` greedy by altitude window, stable w.r.t. existing order
- [ ] Settings: site coords + timezone in ui/state.py, saved to settings.json, editable in a settings dialog (or accept settings-file only for this ticket — decide with existing patterns)
- [ ] Tests: fixed site/date vs astropy reference (twilight within 1 min, transit peak, ordering); settings round-trip; no-gi boundary; no network
- [ ] Full suite green

## Comments

(implementor fills in)
