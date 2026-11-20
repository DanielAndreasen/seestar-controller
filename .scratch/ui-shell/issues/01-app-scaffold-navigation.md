# 01: App scaffold + navigation

**What to build:** the GTK4/libadwaita application skeleton — window with header bar and sidebar navigating between Observe, Enhance, Photometry, Lightcurve, Projects views (placeholders OK), dark theme, window size/position persisted. Demoable: app launches, sidebar switches views, reopening restores window state.

**Blocked by:** seestar-link 01 (uv project scaffold)

**Status:** implemented in 3d31fb0

- [x] App launches as a native GTK4/libadwaita window on the XFCE desktop
- [x] Sidebar switches between the five views
- [x] Dark theme applied
- [x] Window size/position persisted and restored
- [x] Tests: controller-level nav state; headless widget construction smoke test

## Comments

Implemented in the new `src/seestar_controller/ui/` package with a strict two-layer split (sandbox has no GTK — docs/wiki/assumptions.md):

- **Pure layer** `ui/state.py` (stdlib only, NO `gi` import — subprocess-enforced): `AppState` holds the current view, the five views as an ordered `VIEWS` tuple of `View(id, title)` (observe/enhance/photometry/lightcurve/projects), `select_view(id)` with `UnknownViewError` validation, window geometry (`set_window_geometry`/`window_geometry`, width/height bounded, x/y nullable ints), the dark-theme flag (default `True`), and the settings dict (`scope_ip`/`key_path`/`project_root`) with defaults + validation (`SettingsValidationError`). Persistence: one JSON file at `~/.config/seestar-controller/settings.json`, `SEESTAR_SETTINGS_PATH` env override (same explicit-arg > env-var > default precedence as `SEESTAR_KEY_PATH`/`SEESTAR_CACHE_DIR`/`SEESTAR_PROJECT_ROOT`); `load()` never raises on missing/corrupt/wrong-version files (per-field fallback to defaults), `save()` is atomic (mkstemp + fsync + `os.replace`, temp cleaned on any failure — no `*.tmp` leftovers).
- **Widget layer** `ui/views.py` + `ui/app.py` (`gi` imported at module top, never imported by the pure layer or eagerly by `ui/__init__` — widget names are re-exported lazily via PEP 562 `__getattr__`): `SeestarApplication(Adw.Application)` + `SeestarWindow(Adw.ApplicationWindow)` with `Adw.ToolbarView` + `Adw.HeaderBar`. **Sidebar choice (documented in app.py):** a `Gtk.ListBox` (one row per view) in the sidebar of an `Adw.NavigationSplitView`, content side an `Adw.ViewStack` with one page per view — a split view keeps the sidebar visible on a wide desktop window, unlike `Adw.NavigationView` (drill-down pattern). Row selection switches the stack page and updates `AppState` (single source of truth). Views are placeholder `Gtk.Box` + label widgets (`ObserveView` … `ProjectsView`, `VIEW_CLASSES`, `build_view`). Dark theme via `Adw.StyleManager.set_color_scheme(FORCE_DARK/FORCE_LIGHT)` from the settings flag. Geometry restored on start (`set_default_size` + `move` — honored on X11/XFCE, Wayland may ignore) and captured on `close-request` (`get_width/get_height/get_position`) into the pure layer, saved on close.
- **Entry points:** `seestar-controller gui` (new subcommand in `cli.py`; `gi` imported lazily inside the handler so the CLI works without GTK — without the system packages it exits 1 with an actionable message) and `uv run python -m seestar_controller.ui.app`. No `pygobject` in pyproject; module docstrings + README note that the desktop needs system packages `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`.
- **Tests:** `tests/test_ui_state.py` (85 tests, pure, tmp_path only): view list/order, `select_view` valid/invalid, defaults, save/load round-trip, atomic write (no tmp leftovers; failing `os.replace` keeps the original), corrupt/missing/wrong-schema file -> defaults + no crash, per-field fallback, geometry round-trip + validation bounds, dark default True + round-trip + validation, settings validation (IPv4/hostname scope_ip, path settings, unknown key), env-var override (monkeypatch + env mapping), path precedence, subprocess test that importing `seestar_controller.ui`/`.state` does NOT import `gi` (and app/views stay lazy), CLI `gui` without GTK fails cleanly (rc 1, names the system packages). `tests/test_ui_widgets.py`: `pytest.importorskip("gi")` + `require_version` + `Gtk.init_check()` skip guards — view construction, window stack/sidebar switching, style manager, geometry capture/restore, close-request persistence; SKIPS in this sandbox, runs on the desktop.
- `uv run pytest`: 800 passed, 1 skipped (715 baseline + 85 new; the widget module is the 1 skip).
