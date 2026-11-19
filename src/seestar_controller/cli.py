"""Command-line entry point for seestar-controller.

Currently one subcommand:

- ``seestar-controller extract-key <seestar.apk|xapk>`` — extract the interop
  PEM key from the official Seestar Android app package (the same flow as
  seestarpy's ``python -m seestarpy.extract_pem``) and write it to the
  configured key path (``~/.config/seestar-controller/key.pem`` unless
  ``SEESTAR_KEY_PATH`` or ``--key-path`` says otherwise) with mode 0600.

Also runnable as ``uv run python -m seestar_controller extract-key ...``.
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "extract-key":
        return _cmd_extract_key(args)
    if args.command == "key-path":
        print(keys.resolve_key_path())
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
