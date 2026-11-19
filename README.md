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
