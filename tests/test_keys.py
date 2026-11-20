"""PEM key management (ticket 02): config path, load/save, extraction from
the official app package, and transport integration.

No real key / no real app package available (user AFK —
docs/wiki/assumptions.md): generated test keys and synthetic APK fixtures.
"""

import os
import stat
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization

from seestar_controller.link import auth, keys
from seestar_controller.link.keys import (
    InvalidKeyError,
    KeyExtractionError,
    MissingKeyError,
)
from seestar_controller.link.transport import LinkTransport

# -- helpers -----------------------------------------------------------------


def write_fake_apk(
    path,
    private_pem: bytes,
    *,
    so_name: str = "lib/arm64-v8a/libopenssllib.so",
    with_key: bool = True,
) -> object:
    """A synthetic APK: a zip whose OpenSSL native lib embeds the PEM key."""
    payload = b"\x7fELF\x00\x01binary noise\xff\xfe"
    if with_key:
        payload += b"\n" + private_pem + b"\n"
    payload += b"trailing noise\x00\x00"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(so_name, payload)
        zf.writestr("classes.dex", b"dex\n035\x00")
    return path


def write_fake_xapk(path, private_pem: bytes) -> object:
    """A synthetic XAPK: a zip whose base.apk is itself an APK zip."""
    import io

    inner = io.BytesIO()
    write_fake_apk(inner, private_pem)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("base.apk", inner.getvalue())
        zf.writestr("manifest.json", b'{"xapk_version": 1}')
    return path


# -- path resolution -----------------------------------------------------------


def test_resolve_key_path_precedence(tmp_path):
    from pathlib import Path

    from seestar_controller.link.auth import DEFAULT_KEY_PATH

    assert keys.resolve_key_path(None, env={}) == Path(DEFAULT_KEY_PATH)
    env_path = keys.resolve_key_path(None, env={keys.KEY_PATH_ENV_VAR: "/tmp/env.pem"})
    assert env_path == Path("/tmp/env.pem")
    explicit = keys.resolve_key_path(
        "/tmp/explicit.pem", env={keys.KEY_PATH_ENV_VAR: "/tmp/env.pem"}
    )
    assert explicit == Path("/tmp/explicit.pem")


def test_env_var_selects_key_path(tmp_path, monkeypatch, keypair):
    private_pem, _public_pem = keypair
    key_file = tmp_path / "from-env.pem"
    key_file.write_bytes(private_pem)
    monkeypatch.setenv(keys.KEY_PATH_ENV_VAR, str(key_file))
    assert keys.resolve_key_path() == key_file
    key = keys.load_key()  # no explicit path -> env var
    assert auth.verify_challenge(
        auth.load_public_key(keypair[1]),
        "challenge",
        auth.sign_challenge(key, "challenge"),
    )


# -- load ----------------------------------------------------------------------


def test_load_key_success(tmp_path, keypair):
    private_pem, public_pem = keypair
    key_file = keys.save_key(private_pem, tmp_path / "key.pem")
    key = keys.load_key(key_file)
    signature = auth.sign_challenge(key, "hello")
    assert auth.verify_challenge(auth.load_public_key(public_pem), "hello", signature)


def test_missing_key_error_names_extraction_step(tmp_path):
    missing = tmp_path / "no-such-key.pem"
    with pytest.raises(MissingKeyError) as excinfo:
        keys.load_key(missing, env={})
    message = str(excinfo.value)
    assert str(missing) in message
    assert "extract" in message
    assert "seestarpy" in message and "extract_pem" in message
    assert "seestar-controller extract-key" in message
    assert keys.KEY_PATH_ENV_VAR in message


def test_invalid_key_rejected(tmp_path):
    garbage = tmp_path / "key.pem"
    garbage.write_bytes(b"this is not a PEM key\n")
    with pytest.raises(InvalidKeyError) as excinfo:
        keys.load_key(garbage, env={})
    assert "extract" in str(excinfo.value)


def test_public_key_pem_rejected_as_private_key(tmp_path, keypair):
    _private_pem, public_pem = keypair
    key_file = tmp_path / "key.pem"
    key_file.write_bytes(public_pem)
    with pytest.raises(InvalidKeyError) as excinfo:
        keys.load_key(key_file, env={})
    message = str(excinfo.value)
    assert "not a valid private key" in message
    assert "extract" in message


def test_encrypted_private_key_rejected(tmp_path, keypair):
    private_pem, _public_pem = keypair
    key = auth.load_private_key(private_pem)
    encrypted = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(b"secret"),
    )
    key_file = tmp_path / "key.pem"
    key_file.write_bytes(encrypted)
    with pytest.raises(InvalidKeyError):
        keys.load_key(key_file, env={})


# -- save (0600) ---------------------------------------------------------------


def test_save_key_writes_0600_and_creates_dirs(tmp_path, keypair):
    private_pem, _public_pem = keypair
    key_file = keys.save_key(private_pem, tmp_path / "nested" / "dir" / "key.pem")
    assert key_file.is_file()
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert key_file.read_bytes() == private_pem  # already newline-terminated
    assert keys.load_key(key_file) is not None


def test_save_key_tightens_existing_file_permissions(tmp_path, keypair):
    private_pem, _public_pem = keypair
    key_file = tmp_path / "key.pem"
    key_file.write_bytes(b"old contents\n")
    os.chmod(key_file, 0o644)
    keys.save_key(private_pem, key_file)
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert key_file.read_bytes() == private_pem


def test_save_key_rejects_invalid_pem_and_writes_nothing(tmp_path):
    key_file = tmp_path / "key.pem"
    with pytest.raises(InvalidKeyError):
        keys.save_key(b"not a pem", key_file)
    assert not key_file.exists()


# -- extraction from the app package -------------------------------------------


def test_extract_pem_from_apk(tmp_path, keypair):
    private_pem, public_pem = keypair
    apk = write_fake_apk(tmp_path / "Seestar.apk", private_pem)
    extracted = keys.extract_pem_from_package(apk)  # ty: ignore[invalid-argument-type]
    key = auth.load_private_key(extracted)
    signature = auth.sign_challenge(key, "challenge")
    assert auth.verify_challenge(auth.load_public_key(public_pem), "challenge", signature)


def test_extract_pem_from_xapk_scans_nested_base_apk(tmp_path, keypair):
    private_pem, _public_pem = keypair
    xapk = write_fake_xapk(tmp_path / "Seestar.xapk", private_pem)
    extracted = keys.extract_pem_from_package(xapk)  # ty: ignore[invalid-argument-type]
    assert auth.load_private_key(extracted) is not None


def test_extract_pem_fallback_scans_other_entries(tmp_path, keypair):
    private_pem, _public_pem = keypair
    # key in an asset, not in the native lib
    apk = write_fake_apk(
        tmp_path / "Seestar.apk", private_pem, so_name="lib/arm64-v8a/other.so"
    )
    with zipfile.ZipFile(apk, "a") as zf:  # ty: ignore[no-matching-overload]
        zf.writestr("assets/interop.txt", b"prefix " + private_pem + b" suffix")
    extracted = keys.extract_pem_from_package(apk)  # ty: ignore[invalid-argument-type]
    assert auth.load_private_key(extracted) is not None


def test_extract_pem_skips_invalid_blocks(tmp_path, keypair):
    private_pem, _public_pem = keypair
    bogus = (
        b"-----BEGIN PRIVATE KEY-----\n!!!not base64!!!\n"
        b"-----END PRIVATE KEY-----\n"
    )
    payload = b"noise" + bogus + b"\n" + private_pem
    apk = tmp_path / "Seestar.apk"
    with zipfile.ZipFile(apk, "w") as zf:
        zf.writestr("lib/arm64-v8a/libopenssllib.so", payload)
    extracted = keys.extract_pem_from_package(apk)
    assert extracted == private_pem  # cryptography PEM output ends with \n


def test_extract_pem_no_key_in_package_raises_actionable(tmp_path):
    apk = write_fake_apk(tmp_path / "Seestar.apk", b"", with_key=False)
    with pytest.raises(KeyExtractionError) as excinfo:
        keys.extract_pem_from_package(apk)  # ty: ignore[invalid-argument-type]
    message = str(excinfo.value)
    assert "no PEM private key" in message
    assert "seestarpy" in message and "extract_pem" in message


def test_extract_pem_only_invalid_blocks_raises(tmp_path):
    bogus = (
        b"-----BEGIN PRIVATE KEY-----\n!!!not base64!!!\n"
        b"-----END PRIVATE KEY-----\n"
    )
    apk = tmp_path / "Seestar.apk"
    with zipfile.ZipFile(apk, "w") as zf:
        zf.writestr("lib/arm64-v8a/libopenssllib.so", bogus)
    with pytest.raises(KeyExtractionError) as excinfo:
        keys.extract_pem_from_package(apk)
    assert "none is a valid" in str(excinfo.value)


def test_extract_pem_missing_package_raises(tmp_path):
    with pytest.raises(KeyExtractionError) as excinfo:
        keys.extract_pem_from_package(tmp_path / "missing.apk")
    assert "not found" in str(excinfo.value)


def test_extract_pem_not_a_zip_raises(tmp_path):
    fake = tmp_path / "Seestar.apk"
    fake.write_bytes(b"definitely not a zip archive")
    with pytest.raises(KeyExtractionError) as excinfo:
        keys.extract_pem_from_package(fake)
    assert "not an APK/XAPK" in str(excinfo.value)


# -- transport integration -------------------------------------------------------


def test_transport_connect_uses_configured_key_file(mock_scope, keypair, tmp_path, monkeypatch):
    """connect() signs the handshake with the key at the configured path
    (SEESTAR_KEY_PATH), verified by the mock Scope's matching public key."""
    private_pem, _public_pem = keypair
    key_file = tmp_path / "key.pem"
    key_file.write_bytes(private_pem)
    monkeypatch.setenv(keys.KEY_PATH_ENV_VAR, str(key_file))

    t = LinkTransport(
        "127.0.0.1",
        mock_scope.command_port,
        heartbeat_interval=0.1,
        heartbeat_timeout=0.5,
        request_timeout=2.0,
    )
    assert t.key_path == key_file
    t.connect()
    try:
        assert t.connected
        assert t.verified
        assert t.request("get_device_state")["fw_version"] == "9.75"
    finally:
        t.disconnect()


def test_transport_pem_path_param_still_works(mock_scope, keypair, tmp_path):
    private_pem, _public_pem = keypair
    key_file = tmp_path / "explicit.pem"
    key_file.write_bytes(private_pem)
    t = LinkTransport(
        "127.0.0.1",
        mock_scope.command_port,
        pem_path=key_file,
        heartbeat_interval=0.1,
        request_timeout=2.0,
    )
    assert t.key_path == key_file
    t.connect()
    try:
        assert t.verified
    finally:
        t.disconnect()


def test_transport_missing_key_raises_actionable_error(mock_scope, tmp_path, monkeypatch):
    missing = tmp_path / "absent.pem"
    monkeypatch.setenv(keys.KEY_PATH_ENV_VAR, str(missing))
    with pytest.raises(MissingKeyError) as excinfo:
        LinkTransport("127.0.0.1", mock_scope.command_port)
    message = str(excinfo.value)
    assert "extract" in message and "seestarpy" in message
    assert str(missing) in message


def test_transport_invalid_key_file_raises(mock_scope, tmp_path, monkeypatch):
    bad = tmp_path / "key.pem"
    bad.write_bytes(b"garbage, not a key\n")
    monkeypatch.setenv(keys.KEY_PATH_ENV_VAR, str(bad))
    with pytest.raises(InvalidKeyError) as excinfo:
        LinkTransport("127.0.0.1", mock_scope.command_port)
    assert "extract" in str(excinfo.value)
