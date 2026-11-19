"""CLI entry point (ticket 02): `seestar-controller extract-key` extracts the
PEM key from the official app package and writes it to the config path 0600.

Synthetic APK fixtures only — no real app package available (user AFK).
"""

import stat
import zipfile

import pytest

from seestar_controller import cli
from seestar_controller.link import keys


def make_fake_apk(path, private_pem: bytes) -> object:
    payload = b"\x7fELF noise\x00" + b"\n" + private_pem + b"\n" + b"noise"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("lib/arm64-v8a/libopenssllib.so", payload)
        zf.writestr("classes.dex", b"dex\n")
    return path


def test_extract_key_writes_key_file_0600(tmp_path, keypair, capsys):
    private_pem, _public_pem = keypair
    apk = make_fake_apk(tmp_path / "Seestar.apk", private_pem)
    key_file = tmp_path / "cfg" / "key.pem"

    rc = cli.main(["extract-key", str(apk), "--key-path", str(key_file)])

    assert rc == 0
    assert key_file.is_file()
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert key_file.read_bytes() == private_pem
    out = capsys.readouterr().out
    assert str(key_file) in out
    # the written key is usable for the signed handshake
    assert keys.load_key(key_file) is not None


def test_extract_key_uses_env_var_path(tmp_path, keypair, monkeypatch, capsys):
    private_pem, _public_pem = keypair
    apk = make_fake_apk(tmp_path / "Seestar.apk", private_pem)
    env_key = tmp_path / "env-key.pem"
    monkeypatch.setenv(keys.KEY_PATH_ENV_VAR, str(env_key))

    rc = cli.main(["extract-key", str(apk)])

    assert rc == 0
    assert env_key.is_file()
    assert stat.S_IMODE(env_key.stat().st_mode) == 0o600
    assert env_key.read_bytes() == private_pem


def test_extract_key_refuses_to_overwrite_without_force(tmp_path, keypair, capsys):
    private_pem, _public_pem = keypair
    apk = make_fake_apk(tmp_path / "Seestar.apk", private_pem)
    key_file = tmp_path / "key.pem"
    key_file.write_bytes(b"-----BEGIN PRIVATE KEY-----\nexisting\n-----END PRIVATE KEY-----\n")

    rc = cli.main(["extract-key", str(apk), "--key-path", str(key_file)])
    assert rc == 1
    assert "already exists" in capsys.readouterr().err
    assert b"existing" in key_file.read_bytes()  # untouched

    rc = cli.main(["extract-key", str(apk), "--key-path", str(key_file), "--force"])
    assert rc == 0
    assert key_file.read_bytes() == private_pem


def test_extract_key_bad_package_exits_nonzero_with_hint(tmp_path, capsys):
    not_an_apk = tmp_path / "Seestar.apk"
    not_an_apk.write_bytes(b"not a zip")
    rc = cli.main(["extract-key", str(not_an_apk), "--key-path", str(tmp_path / "k.pem")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error" in err
    assert "seestarpy" in err and "extract_pem" in err
    assert not (tmp_path / "k.pem").exists()


def test_extract_key_missing_package_exits_nonzero(tmp_path, capsys):
    rc = cli.main(["extract-key", str(tmp_path / "nope.apk")])
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_key_path_subcommand_prints_resolved_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(keys.KEY_PATH_ENV_VAR, str(tmp_path / "k.pem"))
    assert cli.main(["key-path"]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "k.pem")


def test_no_command_prints_help(capsys):
    assert cli.main([]) == 0
    assert "usage" in capsys.readouterr().out.lower()
