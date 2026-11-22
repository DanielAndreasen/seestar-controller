# Issue 03: observing site (site_latitude/longitude/elevation/timezone) is settings-file only — no GUI field, Observe view errors with VisibilityError

**Status:** needs-triage

## Found while
Live session 2026-11-22: the Observe view shows
`VisibilityError: site latitude is not set ('site_latitude' is empty) - set the observing site in settings.json`.
The `site_*` keys exist in `ui/state.py` (`SETTINGS_SCHEMA`, validators
`validate_site_latitude` etc.) and are persisted, but the Settings dialog's
editable rows (`EDITABLE_SETTINGS_KEYS` in `ui/settings_dialog.py` /
`ui/state.py`) only cover `scope_ip`, `key_path`, `project_root`,
`photometry_comp_search_radius_deg` — the comment says the `site_*` keys are
"settings-file only until the visibility-planner view ticket adds the rows".
A user who installed the app has no way to set the site without hand-editing
settings.json.

## Expected
- The Settings dialog (or a dedicated Site section in it / the Visibility view)
  has editable rows for `site_latitude`, `site_longitude`, `site_elevation_m`,
  `site_timezone`, with the existing validators and per-row error labels.
- Alternatively/additionally: when a view needs the site and it is unset, show an
  inline actionable message ("Open Settings to set your observing site") instead
  of a raw `VisibilityError`.

## Scope
`src/seestar_controller/ui/settings_dialog.py` (FIELD_TITLES + rows),
`src/seestar_controller/ui/state.py` (EDITABLE_SETTINGS_KEYS), possibly the
Observe view error surface, tests in `tests/test_ui_settings*.py` /
`tests/test_visibility_*.py`.
