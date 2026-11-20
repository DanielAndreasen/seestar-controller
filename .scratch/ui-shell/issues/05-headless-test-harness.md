# 05: Headless controller test harness

**What to build:** the test infrastructure that keeps UI logic verifiable without a display — controllers separated from widgets, a harness that runs controller tests headless, and a smoke test that instantiates every view with a dummy GTK backend. Demoable: `uv run pytest` passes with no display.

**Blocked by:** 01

**Status:** implemented in 93fcc8d

- [x] Controller layer holds app logic, widgets stay thin
- [x] Controller tests run headless (no display needed)
- [x] Smoke test constructs every view headless
- [x] CI-style command documented: `uv run pytest`

## Comments

Implemented keeping the strict pure/widget split (sandbox has no GTK — docs/wiki/assumptions.md):

- **Audit (no violations found, now test-enforced):** every module of `seestar_controller/ui` is either pure — `__init__`, `state`, `settings`, `connection`, `notifications` (no `gi` import anywhere; `connection`/`notifications` also keep `link` out of module top) — or a widget module — `app`, `views`, `settings_dialog`, `connection_panel`, `toast`, `notification_log` (`import gi` at module top, before `from gi.repository`, re-exported lazily from `ui/__init__.py` via PEP 562 `_WIDGET_EXPORTS`, which covers exactly the six widget modules). Encoded as two tests in `tests/test_ui_harness.py`: a subprocess test that imports the package + ALL pure modules and asserts `'gi' not in sys.modules` (plus no widget module and no `seestar_controller.link` in `sys.modules`), and a source-scan test classifying every `ui/*.py`.
- **Harness:** `tests/conftest.py` gained `FakeDevice` (duck-typed facade stand-in matching `add_event_handler(name, handler)` / `remove_event_handler(name, handler)`, with an `emit(name, event)` dispatcher — no network, no gi) and the `ui_harness` fixture, which builds the app-level controllers exactly like `SeestarApplication` does — `AppState` (settings file in `tmp_path`), `SettingsController`, `ConnectionController` (fake device factory), `NotificationBus`, `NotificationController` — returned as a namespace. `tests/test_ui_harness.py` proves it headless: connect→CONNECTED→disconnect with the fake device, device events (`Stack`/`scope_error`) translated into bus notifications, `detach()` leaving no listeners, and `SettingsController.update` persisting atomically to the tmp settings file.
- **Smoke test:** `tests/test_ui_widgets.py` (already `pytest.importorskip("gi")` + `Gtk.init_check()` skip) gained `test_smoke_every_view_constructs_and_adds_to_a_view_stack` — constructs EVERY view class from `VIEW_CLASSES`, adds each to an `Adw.ViewStack`, asserts no exception, expected widget types (`Gtk.Box` subclass, `view_id`/`title`), and one page per view id; the window-instantiation tests were already there. Module docstring now carries the dummy-backend note: if `gi` is importable but there is no display, `Gtk.init_check()` returns False and the tests skip; on the desktop they run against a real display or `xvfb-run -a uv run pytest`.
- **Docs:** `README.md` gained a Testing section (`uv sync && uv run pytest` runs the full suite headless; widget tests skip without GTK; on the desktop with `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1` they run, optionally under `xvfb-run`) and the GUI section now names the full pure/widget split; `docs/wiki/workflow.md` gained a Testing (CI-style) section with the same command + the split rule for future ui code.
- `uv run pytest`: 912 passed, 1 skipped (906 baseline + 6 new pure harness tests; the widget module is the 1 skip in this sandbox).
