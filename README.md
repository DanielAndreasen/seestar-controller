# seestar-controller

Desktop controller for the Seestar S50 ("the Scope") over the home LAN.

See `GLOSSARY.md` for the project vocabulary and `docs/wiki/` for decisions
and assumptions. Work is organised as specs + tickets under `.scratch/`.

## Development

Python 3.12, managed with [uv](https://docs.astral.sh/uv/):

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

## GUI (ui-shell)

The desktop app is GTK4/libadwaita via PyGObject. PyGObject is **not** a pip
dependency (it needs the system GTK headers to build), so on the desktop
machine install the system packages first:

```sh
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
uv run seestar-controller gui        # or: uv run python -m seestar_controller.ui.app
```

The `ui` package has a strict two-layer split: `seestar_controller.ui.state`
is the pure-Python layer (app state, navigation, settings — no `gi` import,
fully unit-tested headless), while `ui.app` / `ui.views` are the widget
layer (`gi` imported at module top, reachable only via the `gui` subcommand,
which imports it lazily so the CLI works without GTK).
