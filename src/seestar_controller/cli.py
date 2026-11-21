"""Command-line entry point for seestar-controller.

Subcommands:

- ``seestar-controller extract-key <seestar.apk|xapk>`` — extract the interop
  PEM key from the official Seestar Android app package (the same flow as
  seestarpy's ``python -m seestarpy.extract_pem``) and write it to the
  configured key path (``~/.config/seestar-controller/key.pem`` unless
  ``SEESTAR_KEY_PATH`` or ``--key-path`` says otherwise) with mode 0600.
- ``seestar-controller key-path`` — print the resolved PEM key path.
- ``seestar-controller gui`` — launch the GTK4/libadwaita desktop app
  (``seestar_controller.ui.app``). The ``gi`` import happens lazily inside
  this subcommand, so the CLI works on machines without GTK. Since the
  Python 3.14 bump the system PyGObject (cp312-only) no longer loads in the
  3.14 venv; get a working ``gi`` for 3.14 via one of (see
  docs/wiki/dependency-upgrade.md, issue-06 entry, 2026-11-21):

  - system python3 is 3.14: ``sudo apt install python3-gi gir1.2-gtk-4.0
    gir1.2-adw-1`` then ``uv venv --system-site-packages --clear .venv &&
    uv sync``;
  - otherwise: ``sudo apt install build-essential pkg-config
    libgirepository-2.0-dev libcairo2-dev`` then ``uv sync --extra gui``
    (builds PyGObject from source into the venv; there is no cp314 wheel on
    PyPI). PyGObject is never a hard dependency, so headless ``uv sync`` is
    unaffected.

Also runnable as ``uv run python -m seestar_controller ...``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .link import keys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seestar-controller",
        description="Desktop controller for the Seestar S50 (the Scope).",
    )
    sub = parser.add_subparsers(dest="command")

    extract = sub.add_parser(
        "extract-key",
        help="extract the PEM key from the official Seestar Android app package",
        description=(
            "Extract the interop PEM key from the official Seestar Android "
            "app package (APK, or an XAPK whose base APK is scanned) — the "
            "same extraction as `python -m seestarpy.extract_pem <apk>` — "
            "and write it to the configured key path with mode 0600."
        ),
    )
    extract.add_argument(
        "package",
        type=Path,
        help="path to the official Seestar app package (.apk or .xapk)",
    )
    extract.add_argument(
        "--key-path",
        type=Path,
        default=None,
        help=(
            "where to write the key (default: $SEESTAR_KEY_PATH if set, else "
            "~/.config/seestar-controller/key.pem)"
        ),
    )
    extract.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing key file",
    )

    sub.add_parser(
        "key-path",
        help="print the resolved PEM key path",
    )

    sub.add_parser(
        "gui",
        help="launch the GTK4/libadwaita desktop app",
        description=(
            "Launch the GTK4/libadwaita desktop app (seestar_controller.ui.app). "
            "Needs a PyGObject (gi) built for the venv's Python 3.14: either the "
            "system packages python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 on a 3.14 "
            "system python + `uv venv --system-site-packages --clear .venv && "
            "uv sync`, or `uv sync --extra gui` (source build; no cp314 wheel on "
            "PyPI). PyGObject is never a hard dependency — see "
            "docs/wiki/dependency-upgrade.md (issue-06 entry, 2026-11-21)."
        ),
    )
    return parser


def _cmd_extract_key(args: argparse.Namespace) -> int:
    try:
        pem = keys.extract_pem_from_package(args.package)
    except keys.KeyExtractionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    key_path = keys.resolve_key_path(args.key_path)
    if key_path.exists() and not args.force:
        print(
            f"error: a key already exists at {key_path} — not overwriting "
            f"(use --force to replace it)",
            file=sys.stderr,
        )
        return 1
    try:
        written = keys.save_key(pem, args.key_path)
    except keys.InvalidKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"extracted PEM key from {args.package} and wrote it to {written} "
        f"(mode {oct(keys.KEY_FILE_MODE & 0o777)})"
    )
    return 0


def _gui_missing_gi_message(exc: BaseException) -> str:
    """Clean, actionable error for `gui` when no 3.14-built PyGObject is found.

    Names the chosen fix (docs/wiki/dependency-upgrade.md, issue-06 entry,
    2026-11-21): system packages on a 3.14 system python + system-site-packages
    venv, else the `gui` extra which builds PyGObject from source into the venv
    (no cp314 wheel exists on PyPI).
    """
    return (
        "error: the GUI needs PyGObject (`gi`) built for this venv's Python "
        "3.14 — the system PyGObject is cp312-only and cannot load here "
        f"({exc}). Fix, cheapest first (docs/wiki/dependency-upgrade.md, "
        "issue-06 entry, 2026-11-21):\n"
        "  A) system python3 is 3.14:\n"
        "       sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1\n"
        "       uv venv --system-site-packages --clear .venv && uv sync\n"
        "  B) otherwise (no cp314 wheel on PyPI — PyGObject is sdist-only):\n"
        "       sudo apt install build-essential pkg-config "
        "libgirepository-2.0-dev libcairo2-dev\n"
        "       uv sync --extra gui\n"
        "     (equivalently: uv pip install --python .venv/bin/python "
        "pygobject pycairo)\n"
        "PyGObject is never a hard dependency, so headless `uv sync` and the "
        "rest of the CLI are unaffected."
    )


def _cmd_gui() -> int:
    """Launch the GTK4/libadwaita GUI.

    The ``gi`` import happens here (lazily), so the rest of the CLI works on
    machines without GTK — headless sandboxes, CI. Without a 3.14-built
    PyGObject this fails cleanly with an actionable message instead of a
    traceback.
    """
    try:
        from .ui.app import main as gui_main
    except (ImportError, ValueError) as exc:
        print(_gui_missing_gi_message(exc), file=sys.stderr)
        return 1
    return gui_main()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "extract-key":
        return _cmd_extract_key(args)
    if args.command == "key-path":
        print(keys.resolve_key_path())
        return 0
    if args.command == "gui":
        return _cmd_gui()
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
