# seestar-controller

Desktop controller for the Seestar S50 ("the Scope") over the home LAN —
capture, photometry, astrometry, and analysis in one app, with a headless
CLI and an optional GTK4 GUI.

Vocabulary lives in `GLOSSARY.md`; decisions and assumptions in `docs/wiki/`;
work is organised as specs + tickets under `.scratch/`.

## Quickstart

Requires Python 3.14, managed with [uv](https://docs.astral.sh/uv/):

```sh
uv sync                                  # venv + dependencies (headless-safe)
uv run seestar-controller extract-key ~/Seestar.apk   # once: PEM key from the official app
uv run seestar-controller gui            # desktop app (needs GTK, see below)
uv run pytest -q -n auto                 # full test suite, headless
```

Also runnable as `uv run python -m seestar_controller ...`.

## What's inside

| Area | Module | Highlights |
|---|---|---|
| Scope link | `link` | JSON-RPC on TCP :4700 + image sockets :4800/:4804, firmware >= 7.18 PEM-signed handshake, `mock_scope` for testing without hardware |
| Observing | `observing` | capture executor, target queue / nightly plan |
| Projects | `store` | on-disk project DB: frames, stacks, measurements, presets, CSV export |
| Photometry | `photometry` | aperture photometry on sub-frames vs comp ensemble, star picking, run sessions |
| Astrometry | `astrometry` | WCS solving, drift, annotation, session batch |
| Analysis | `analysis` | lightcurves, periodograms, phase folding, export |
| Modeling | `transit`, `variable_scan` | transit fits, variable-star scans |
| Imaging | `stacking`, `mosaic`, `enhance` | stack/mosaic combination, non-destructive enhance pipeline |
| Quality | `frame_quality` | per-frame metrics, session trends |
| Sky data | `catalog`, `gaia`, `moving_objects` | SIMBAD/catalog cache, Gaia crossmatch, moving-object detection + MPC |
| Diagrams | `hr_diagram` | HR diagram with isochrone fitting |
| Planning | `visibility` | target visibility timelines, site config |
| Reports | `report` | HTML/PDF session reports with plots |
| GUI | `ui` | GTK4/libadwaita desktop app (see below) |

## GUI

The desktop app is GTK4/libadwaita via PyGObject. PyGObject is **not** a pip
dependency, so install system GTK first, one of:

```sh
# system python3 is 3.14:
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
uv venv --system-site-packages --clear .venv && uv sync

# otherwise (builds PyGObject from source; no cp314 wheel on PyPI):
sudo apt install build-essential pkg-config libgirepository-2.0-dev libcairo2-dev
uv sync --extra gui
```

Then `uv run seestar-controller gui`. Details: `docs/wiki/dependency-upgrade.md`.

The `ui` package has a strict two-layer split: pure-Python modules
(`ui.state`, `ui.settings`, `ui.connection`, `ui.notifications` — no `gi`
import, fully unit-testable headless) hold all app logic; widget modules
(`ui.app`, `ui.views`, …) import `gi` at module top and are re-exported
lazily from `ui/__init__.py` (PEP 562), so `import seestar_controller.ui`
never imports GTK and the CLI works without it. A subprocess test enforces
the boundary.

## Testing

```sh
uv run pytest -q -n auto                  # full suite, parallel, headless
uv run pytest -q -n auto -m "not slow"    # fast loop (skips slow integration tests)
xvfb-run -a uv run pytest                 # on a desktop: also runs the GTK widget tests
```

Widget tests guard with `pytest.importorskip("gi")` + `Gtk.init_check()` and
skip when GTK or a display is unavailable. Rule for future ui code: pure
modules never import `gi`; widget modules do, lazily exported from the
package. Controller tests build app-level controllers headless via the
`ui_harness` fixture in `tests/conftest.py` (fakes only — no network, no gi).

## Development

- Lint/typecheck before committing: `uv tool run ruff check` and `uv tool run ty check`.
- Each ticket gets its own commit on its own branch off `main`, developed in a
  git worktree under `~/.vibe/worktrees/`; see `AGENTS.md`.
