"""PEM key management for the Link: where the key lives, how it is loaded,
and how it is extracted from the official Seestar Android app.

The firmware >= 7.18 (incl. 9.75) signed handshake needs the "interop PEM"
RSA private key that every copy of the official Seestar Android app
embeds. It is extracted **once** from the app package (APK, or the base APK
inside an XAPK) — the same flow as seestarpy's extractor
(``python -m seestarpy.extract_pem <seestar.apk>``) — and stored locally at
``~/.config/seestar-controller/key.pem`` with mode 0600.

The path is configurable:

1. an explicit ``path`` argument (e.g. from app settings),
2. the ``SEESTAR_KEY_PATH`` environment variable (same variable seestarpy
   auto-discovers),
3. the default :data:`~seestar_controller.link.auth.DEFAULT_KEY_PATH`.

Missing or invalid keys raise :class:`MissingKeyError` /
:class:`InvalidKeyError` with an actionable message that names the
extraction step, so the UI can show the user exactly what to do.

NOTE (user AFK, no real key available — docs/wiki/assumptions.md): loading,
saving, and extraction are tested with generated *test* keys and synthetic
APK fixtures only; the extraction flow mirrors seestarpy's documented one
but is unverified against a real app package.
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from pathlib import Path
from typing import Any, Mapping

from cryptography.hazmat.primitives.asymmetric import rsa

from .auth import DEFAULT_KEY_PATH, load_private_key

#: Environment variable that overrides the default key path (same name
#: seestarpy auto-discovers).
KEY_PATH_ENV_VAR = "SEESTAR_KEY_PATH"

#: File mode for the key file: owner read/write only.
KEY_FILE_MODE = 0o600

#: Name of the native library inside the official app that carries the key.
OPENSSL_LIB_NAME = "libopenssllib.so"

#: Cap on a single zip entry read during extraction (the .so is a few MiB).
MAX_ENTRY_BYTES = 64 * 1024 * 1024

#: A PEM private-key block, e.g. ``-----BEGIN PRIVATE KEY-----`` (PKCS#8,
#: what the app embeds) or ``-----BEGIN RSA PRIVATE KEY-----`` (PKCS#1).
PEM_PRIVATE_KEY_RE = re.compile(
    rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
    rb"-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)

#: How to get the key, named in every actionable error message.
EXTRACTION_HINT = (
    "extract the interop PEM key from the official Seestar Android app "
    "(APK, or the base APK inside an XAPK) with seestarpy's extractor — "
    "`python -m seestarpy.extract_pem <seestar.apk>` — or run "
    "`uv run seestar-controller extract-key <seestar.apk>`, which writes "
    "the key to the configured path"
)


class KeyManagementError(Exception):
    """Base class for key-management failures."""


class MissingKeyError(KeyManagementError):
    """No PEM key file at the configured path."""


class InvalidKeyError(KeyManagementError):
    """The key file exists but is not a usable unencrypted RSA private key."""


class KeyExtractionError(KeyManagementError):
    """The PEM key could not be extracted from an app package."""


def resolve_key_path(
    path: str | Path | None = None, *, env: Mapping[str, str] | None = None
) -> Path:
    """Resolve the configured key path.

    Precedence: explicit ``path`` > ``SEESTAR_KEY_PATH`` env var > the
    default ``~/.config/seestar-controller/key.pem``.
    """
    if path is not None:
        return Path(path)
    environ = os.environ if env is None else env
    value = environ.get(KEY_PATH_ENV_VAR)
    if value:
        return Path(value)
    return Path(DEFAULT_KEY_PATH)


def load_key(
    path: str | Path | None = None, *, env: Mapping[str, str] | None = None
) -> Any:
    """Load and validate the PEM private key from the configured path.

    Returns the RSA private key object (usable with
    :func:`seestar_controller.link.auth.sign_challenge`).

    Raises :class:`MissingKeyError` if the file does not exist and
    :class:`InvalidKeyError` if it is not an unencrypted RSA private key;
    both messages name the extraction step.
    """
    key_path = resolve_key_path(path, env=env)
    if not key_path.is_file():
        raise MissingKeyError(
            f"no PEM key at {key_path} — {EXTRACTION_HINT} "
            f"(path configurable via the {KEY_PATH_ENV_VAR} environment "
            f"variable or app settings)"
        )
    pem = key_path.read_bytes()
    return _validate_private_key_pem(pem, key_path)


def _validate_private_key_pem(pem: bytes, source: Path | str) -> Any:
    """Parse ``pem`` and require an unencrypted RSA private key."""
    try:
        key = load_private_key(pem)
    except Exception as exc:
        raise InvalidKeyError(
            f"the PEM key at {source} is not a valid private key ({exc}) — "
            f"re-{EXTRACTION_HINT}"
        ) from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise InvalidKeyError(
            f"the PEM key at {source} is a {type(key).__name__}, not an RSA "
            f"private key — re-{EXTRACTION_HINT}"
        )
    return key


def save_key(
    pem: bytes,
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    mode: int = KEY_FILE_MODE,
) -> Path:
    """Validate ``pem`` and write it to the configured key path with 0600.

    Parent directories are created; an existing file is truncated and its
    mode tightened to ``mode`` (default 0600). Returns the written path.
    Raises :class:`InvalidKeyError` (writing nothing) if ``pem`` is not a
    valid unencrypted RSA private key.
    """
    key_path = resolve_key_path(path, env=env)
    _validate_private_key_pem(pem, key_path)  # never write an unusable key
    if not pem.endswith(b"\n"):
        pem += b"\n"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    # O_CREAT mode is masked by the umask, so chmod explicitly afterwards to
    # guarantee 0600 even when the file already existed with looser perms.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    os.chmod(key_path, mode)
    return key_path


# -- extraction from the official app package -------------------------------


def _find_pem_blocks(data: bytes) -> list[bytes]:
    """All PEM private-key blocks in ``data`` (strings-style scan)."""
    return [match.group(0) for match in PEM_PRIVATE_KEY_RE.finditer(data)]


def _is_valid_private_key_pem(block: bytes) -> bool:
    try:
        key = load_private_key(block)
    except Exception:
        return False
    return isinstance(key, rsa.RSAPrivateKey)


def _read_entry(zf: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        info = zf.getinfo(name)
        if info.file_size > MAX_ENTRY_BYTES:
            return None
        return zf.read(name)
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return None


def _scan_zip_for_pem(zf: zipfile.ZipFile, *, nested_apks: bool) -> list[bytes]:
    """Collect PEM blocks from an open APK zip.

    Scans the bundled OpenSSL native library ``lib/<abi>/libopenssllib.so``
    first (where the app keeps the key), then — as a fallback — every other
    entry. With ``nested_apks``, also recurses into nested ``.apk`` entries
    (an XAPK is a zip of APKs).
    """
    names = zf.namelist()
    blocks: list[bytes] = []
    so_entries = [
        name
        for name in names
        if name.startswith("lib/") and name.endswith(OPENSSL_LIB_NAME)
    ]
    for name in so_entries:
        data = _read_entry(zf, name)
        if data:
            blocks.extend(_find_pem_blocks(data))
    if not blocks and nested_apks:
        for name in names:
            if not name.lower().endswith(".apk"):
                continue
            inner_bytes = _read_entry(zf, name)
            if not inner_bytes:
                continue
            try:
                inner = zipfile.ZipFile(io.BytesIO(inner_bytes))
            except zipfile.BadZipFile:
                continue
            with inner:
                blocks.extend(_scan_zip_for_pem(inner, nested_apks=False))
    if not blocks:
        # Fallback: strings-style scan of every entry (the key may sit in an
        # asset or resource rather than the native lib in some app versions).
        for name in names:
            if name.lower().endswith(".apk"):
                continue  # nested APKs handled above
            data = _read_entry(zf, name)
            if data:
                blocks.extend(_find_pem_blocks(data))
    # Dedupe (the same key ships in every ABI variant of the .so).
    return list(dict.fromkeys(blocks))


def extract_pem_from_package(package: str | Path) -> bytes:
    """Extract the interop PEM key from an official Seestar app package.

    Implements the documented extraction flow (the same one seestarpy's
    ``extract_pem`` performs): open the APK as a ZIP — an XAPK works too,
    its nested base APK is scanned — and strings-scan the bundled OpenSSL
    native library ``lib/<abi>/libopenssllib.so`` for a PEM private-key
    block. The first block that parses as an unencrypted RSA private key is
    returned as PEM bytes.

    Raises :class:`KeyExtractionError` with an actionable message if the
    package is missing, is not a zip, or contains no usable key.
    """
    path = Path(package)
    if not path.is_file():
        raise KeyExtractionError(
            f"app package not found: {path} — obtain the official Seestar "
            f"Android app package (APK, or the XAPK from which the base APK "
            f"is taken); {EXTRACTION_HINT}"
        )
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise KeyExtractionError(
            f"{path} is not an APK/XAPK package (not a zip archive): {exc} — "
            f"{EXTRACTION_HINT}"
        ) from exc
    with zf:
        blocks = _scan_zip_for_pem(zf, nested_apks=True)
    for block in blocks:
        if _is_valid_private_key_pem(block):
            return block if block.endswith(b"\n") else block + b"\n"
    if blocks:
        raise KeyExtractionError(
            f"found {len(blocks)} PEM block(s) in {path} but none is a valid "
            f"unencrypted RSA private key — re-{EXTRACTION_HINT}"
        )
    raise KeyExtractionError(
        f"no PEM private key found in {path} (scanned lib/*/{OPENSSL_LIB_NAME} "
        f"and all other entries) — the key is embedded in the official Seestar "
        f"Android app; {EXTRACTION_HINT}"
    )
