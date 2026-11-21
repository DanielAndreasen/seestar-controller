# seestar-controller

Desktop controller for the Seestar S50 ("the Scope") over the home LAN.

See `GLOSSARY.md` for the project vocabulary and `docs/wiki/` for decisions
and assumptions. Work is organised as specs + tickets under `.scratch/`.

## Development

Python 3.14, managed with [uv](https://docs.astral.sh/uv/):

```sh
uv sync          # create the venv and install dependencies
uv run pytest    # run the test suite
```

Package layout is `src/`-based; the current slice is
`seestar_controller.link` — the Link (control connection to the Scope):
JSON-RPC on TCP :4700 plus image sockets :4800 (tele) and :4804 (wide),
with the firmware >= 7.18 PEM-signed handshake. A mock Scope speaking the
same wire protocol lives in `seestar_controller.link.mock_scope` so the
whole app is testable without the physical unit.

## Testing

```sh
uv sync && uv run pytest    # the full suite, headless — no display needed
```

The suite is green with no display: all UI logic lives in the pure-Python
modules of `seestar_controller.ui` (`state.py`, `settings.py`,
`connection.py`, `notifications.py` — no `gi` import, enforced by a
subprocess test in `tests/test_ui_harness.py`), and the widget modules
(`app.py`, `views.py`, `settings_dialog.py`, `connection_panel.py`,
`toast.py`, `notification_log.py`) import `gi` at module top and are
re-exported lazily from `ui/__init__.py` (PEP 562), so
`import seestar_controller.ui` never imports GTK. The widget smoke tests
(`tests/test_ui_widgets.py`) guard with `pytest.importorskip("gi")` plus
`Gtk.init_check()` and **skip** when GTK or a display is unavailable.

On the desktop — with `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1` installed —
the widget tests run too, against a real display or a virtual one:

```sh
xvfb-run -a uv run pytest
```

Rule for future ui code: pure modules never import `gi`; widget modules
import `gi` at module top and are lazy-exported from the package; the
boundary is enforced by the subprocess test. Controller tests build the
app-level controllers headless via the `ui_harness` fixture in
`tests/conftest.py` (fakes only — no network, no gi).

## GUI (ui-shell)

The desktop app is GTK4/libadwaita via PyGObject. PyGObject is **not** a pip
dependency (it needs the system GTK headers to build), so on the desktop
machine install the system packages first:

```sh
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
uv run seestar-controller gui        # or: uv run python -m seestar_controller.ui.app
```

The `ui` package has a strict two-layer split: the pure-Python layer
(`ui.state`, `ui.settings`, `ui.connection`, `ui.notifications` — no `gi`
import, fully unit-tested headless; a subprocess test enforces the
boundary) holds all app logic, while the widget layer (`ui.app`, `ui.views`,
`ui.settings_dialog`, `ui.connection_panel`, `ui.toast`,
`ui.notification_log` — `gi` imported at module top, re-exported lazily via
PEP 562) is reachable only via the `gui` subcommand, which imports it lazily
so the CLI works without GTK.
