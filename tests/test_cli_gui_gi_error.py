"""CLI `gui` subcommand gi/PyGObject error path (dependency-upgrade issue 06).

Since the Python 3.14 bump the system PyGObject (cp312-only) cannot load in
the 3.14 venv, so `seestar-controller gui` must fail cleanly with a message
naming the chosen fix (docs/wiki/dependency-upgrade.md, issue-06 entry,
2026-11-21): system packages on a 3.14 system python + system-site-packages
venv (option A), else the optional `gui` extra which builds PyGObject from
source into the venv (option B — no cp314 wheel exists on PyPI).

Pure tests: no gi import, no network. The import of `seestar_controller.ui.app`
is forced to fail (or faked) via `sys.modules`, so these run identically on
headless machines and on desktops where gi IS installed.
"""

import sys
import tomllib
import types
from pathlib import Path

import pytest

from seestar_controller import cli

REPO_ROOT = Path(__file__).resolve().parent.parent


def _block_app_import(monkeypatch):
    """Make `from .ui.app import main` raise ImportError, as when gi is missing."""
    monkeypatch.setitem(sys.modules, "seestar_controller.ui.app", None)


def test_gui_without_3_14_gi_fails_cleanly_naming_both_fixes(monkeypatch, capsys):
    _block_app_import(monkeypatch)

    rc = cli.main(["gui"])

    assert rc == 1
    err = capsys.readouterr().err
    # no traceback, a clean error line first
    assert err.startswith("error:")
    assert "Traceback" not in err
    # names the underlying cause
    assert "gi" in err
    # option A: system packages on a 3.14 system python + system-site-packages venv
    assert "python3-gi" in err
    assert "gir1.2-gtk-4.0" in err
    assert "gir1.2-adw-1" in err
    assert "uv venv --system-site-packages --clear .venv && uv sync" in err
    # option B: source build via the optional gui extra (no cp314 wheel on PyPI)
    assert "libgirepository-2.0-dev" in err
    assert "libcairo2-dev" in err
    assert "uv sync --extra gui" in err
    # points at the dated wiki entry
    assert "docs/wiki/dependency-upgrade.md" in err


def test_gui_missing_gi_message_helper_directly():
    msg = cli._gui_missing_gi_message(ImportError("No module named 'gi'"))
    assert "No module named 'gi'" in msg
    assert "uv sync --extra gui" in msg
    assert "python3-gi" in msg


def test_gui_launches_and_propagates_exit_code_when_gi_importable(monkeypatch, capsys):
    """When `ui.app` imports fine, `gui` runs its main() and returns its code."""
    fake_app = types.ModuleType("seestar_controller.ui.app")
    calls = []

    def fake_main() -> int:
        calls.append(1)
        return 42

    fake_app.__dict__["main"] = fake_main
    monkeypatch.setitem(sys.modules, "seestar_controller.ui.app", fake_app)

    rc = cli.main(["gui"])

    assert rc == 42
    assert calls == [1]
    assert capsys.readouterr().err == ""


def test_gui_help_mentions_the_3_14_gi_fixes(capsys):
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["gui", "--help"])
    assert excinfo.value.code == 0
    # argparse wraps help to the terminal width — normalize whitespace first
    out = " ".join(capsys.readouterr().out.split())
    assert "uv sync --extra gui" in out
    assert "system-site-packages" in out


def test_pygobject_is_optional_extra_never_hard_dependency():
    """Headless installs must keep working: pygobject/pycairo live ONLY in the
    opt-in `gui` extra, never in the hard `dependencies` list."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    hard = pyproject["project"]["dependencies"]
    assert not any("pygobject" in d or "pycairo" in d for d in hard)
    extras = pyproject["project"]["optional-dependencies"]
    assert "gui" in extras
    gui = extras["gui"]
    assert any("pygobject" in d for d in gui)
    assert any("pycairo" in d for d in gui)


def test_lockfile_gates_gui_extra_behind_extra_marker():
    """uv.lock must record pygobject/pycairo only under `extra == 'gui'`, so a
    plain `uv sync` never builds/installs them (headless path unchanged)."""
    lock = (REPO_ROOT / "uv.lock").read_text()
    assert 'name = "pygobject"' in lock
    assert 'name = "pycairo"' in lock
    assert "extra == 'gui'" in lock
