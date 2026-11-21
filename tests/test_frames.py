"""Frame downloads against the mock Scope + atomic-write unit tests.

Covers ticket 06: sub-frames and stacks are downloaded into a caller-given
directory as ``.fits`` + ``.json`` sidecar (time/exposure/gain/target),
written atomically (temp + rename), served by the mock Scope's
``list_frames`` / ``get_frame`` RPCs with astropy-generated FITS bytes.
"""

import base64
import io
import json
import os
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from astropy.io import fits as pyfits

from seestar_controller.link import frames
from seestar_controller.link.device import Device
from seestar_controller.link.frames import (
    GET_FRAME_METHOD,
    KIND_STACK,
    KIND_SUB,
    LIST_FRAMES_METHOD,
    DownloadedFrame,
    FrameMeta,
)
from seestar_controller.link.mock_scope import make_fits_bytes
from seestar_controller.link.transport import RpcError


@pytest.fixture()
def device(transport, mock_scope):
    """A Device facade on the connected transport fixture, fast mock slew."""
    mock_scope.slew_duration = 0.05
    d = Device.connect("127.0.0.1", transport=transport, goto_timeout=2.0)
    yield d
    d.disconnect()


def _read_sidecar(path: Path) -> dict:
    return json.loads(path.read_text())


def _assert_valid_fits(path: Path, sidecar: dict) -> None:
    """The downloaded file is a valid FITS whose headers match the sidecar."""
    with pyfits.open(path) as hdul:
        header = hdul[0].header
        assert str(header["DATE-OBS"]).strip() == sidecar["time"]
        assert float(header["EXPTIME"]) == sidecar["exposure"]
        assert int(header["GAIN"]) == sidecar["gain"]
        assert hdul[0].data is not None


# -- wire level: list_frames / get_frame --------------------------------------


def test_list_frames_empty_initially(transport):
    result = transport.request(LIST_FRAMES_METHOD, {"kind": KIND_SUB})
    assert result == {"frames": []}


def test_add_frame_and_list_over_wire(transport, mock_scope):
    target = {"ra": 1.0, "dec": 2.0}
    r1 = mock_scope.add_frame(KIND_SUB, exposure=10.0, gain=80, target=target)
    mock_scope.add_frame(KIND_SUB, exposure=30.0, gain=50, target=target)
    r3 = mock_scope.add_frame(KIND_STACK, exposure=60.0, gain=80, target=target)
    assert r1["id"] == "sub-0001" and r3["id"] == "stack-0003"
    assert "fits" not in r1  # list records carry metadata, not the FITS bytes

    subs = transport.request(LIST_FRAMES_METHOD, {"kind": "sub"})["frames"]
    assert [r["id"] for r in subs] == ["sub-0001", "sub-0002"]
    assert subs[0]["exposure"] == 10.0 and subs[0]["gain"] == 80
    assert subs[0]["target"] == {"ra": 1.0, "dec": 2.0}
    assert subs[0]["size"] > 0 and subs[0]["time"] and subs[0]["epoch"]

    all_frames = transport.request(LIST_FRAMES_METHOD, {})["frames"]
    assert len(all_frames) == 3
    stacks = transport.request(LIST_FRAMES_METHOD, {"kind": "stack"})["frames"]
    assert [r["id"] for r in stacks] == ["stack-0003"]


def test_list_frames_rejects_bad_params_on_wire(transport):
    with pytest.raises(RpcError):
        transport.request(LIST_FRAMES_METHOD, {"kind": "bogus"})
    with pytest.raises(RpcError):
        transport.request(LIST_FRAMES_METHOD, {"kind": "sub", "since": "not-a-number"})


def test_get_frame_returns_base64_fits(transport, mock_scope):
    record = mock_scope.add_frame(
        KIND_SUB, exposure=10.0, gain=80, target={"ra": 1.0, "dec": 2.0}
    )
    detail = transport.request(GET_FRAME_METHOD, {"id": record["id"]})
    assert detail["id"] == record["id"]
    blob = base64.b64decode(detail["fits_b64"], validate=True)
    assert blob == mock_scope.get_frame(record["id"])["fits"]
    assert blob.startswith(b"SIMPLE")  # FITS magic
    with pyfits.open(io.BytesIO(blob)) as hdul:
        assert str(hdul[0].header["DATE-OBS"]) == record["time"]


def test_get_frame_unknown_id_raises_rpc_error(transport):
    with pytest.raises(RpcError):
        transport.request(GET_FRAME_METHOD, {"id": "sub-9999"})
    with pytest.raises(RpcError):
        transport.request(GET_FRAME_METHOD, {})


# -- downloads through the facade ----------------------------------------------


def test_download_subs_writes_fits_and_sidecars(device, mock_scope, tmp_path):
    device.goto(5.5, 12.25)  # frames record the current target
    for _ in range(3):
        mock_scope.add_frame(KIND_SUB, exposure=10.0, gain=80)

    downloaded = device.download_frames(tmp_path, kind=KIND_SUB)

    assert len(downloaded) == 3
    assert len(list(tmp_path.glob("*.fits"))) == 3
    assert len(list(tmp_path.glob("*.json"))) == 3
    assert not list(tmp_path.glob("*.tmp"))  # no temp leftovers
    for d in downloaded:
        assert isinstance(d, DownloadedFrame)
        assert d.path.exists() and d.sidecar.exists()
        assert d.path.suffix == ".fits" and d.sidecar.suffix == ".json"
        assert d.sidecar.name == d.path.name.replace(".fits", ".json")
        sidecar = _read_sidecar(d.sidecar)
        # sidecar carries the required metadata
        for key in ("time", "exposure", "gain", "target"):
            assert key in sidecar
        assert sidecar["id"] == d.meta.id and sidecar["kind"] == "sub"
        assert sidecar["time"] == d.meta.time
        assert sidecar["exposure"] == 10.0
        assert sidecar["gain"] == 80
        assert sidecar["target"] == {"ra": 5.5, "dec": 12.25}
        assert sidecar["size"] == d.path.stat().st_size
        assert "downloaded_at" in sidecar
        _assert_valid_fits(d.path, sidecar)  # astropy reads it, headers match


def test_download_stacks_writes_fits_and_sidecars(device, mock_scope, tmp_path):
    device.goto(17.0, 34.5)
    mock_scope.add_frame(KIND_STACK, exposure=120.0, gain=60)
    mock_scope.add_frame(KIND_STACK, exposure=240.0, gain=60)

    downloaded = device.download_frames(tmp_path, kind=KIND_STACK)

    assert [d.meta.id for d in downloaded] == ["stack-0001", "stack-0002"]
    for d in downloaded:
        sidecar = _read_sidecar(d.sidecar)
        assert sidecar["kind"] == "stack"
        assert sidecar["gain"] == 60
        assert sidecar["target"] == {"ra": 17.0, "dec": 34.5}
        _assert_valid_fits(d.path, sidecar)
    assert len(list(tmp_path.glob("*.fits"))) == 2


def test_download_since_filters_by_time(device, mock_scope, tmp_path):
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    mock_scope.add_frame(KIND_SUB, when=base)
    mock_scope.add_frame(KIND_SUB, when=base + timedelta(minutes=10))
    mock_scope.add_frame(KIND_STACK, when=base + timedelta(minutes=20))

    # since as datetime
    got = device.download_frames(
        tmp_path, kind=KIND_SUB, since=base + timedelta(minutes=5)
    )
    assert [d.meta.id for d in got] == ["sub-0002"]
    # since as ISO-8601 string
    got = device.download_frames(
        tmp_path, kind=KIND_SUB, since=(base + timedelta(minutes=5)).isoformat()
    )
    assert [d.meta.id for d in got] == ["sub-0002"]
    # since as epoch seconds — nothing that late
    got = device.download_frames(
        tmp_path, kind=KIND_SUB, since=(base + timedelta(minutes=15)).timestamp()
    )
    assert got == []
    # the stack is unaffected by the sub filter
    got = device.download_frames(tmp_path, kind=KIND_STACK)
    assert [d.meta.id for d in got] == ["stack-0003"]


def test_download_invalid_kind_raises_and_writes_nothing(device, tmp_path):
    with pytest.raises(ValueError):
        device.download_frames(tmp_path, kind="bogus")
    assert list(tmp_path.iterdir()) == []


def test_download_returns_empty_when_no_frames(device, tmp_path):
    assert device.download_frames(tmp_path, kind=KIND_SUB) == []
    assert device.download_frames(tmp_path, kind=KIND_STACK) == []


def test_stacking_produces_downloadable_subs_and_stack(device, mock_scope, tmp_path):
    """End to end: stack on the mock, then download the sub-frames and the
    Stack the scope produced."""
    mock_scope.stack_event_interval = 0.05
    device.goto(3.0, 4.0)
    device.start_stack(10.0, 80)
    time.sleep(0.3)
    device.stop_stack()

    subs = device.download_frames(tmp_path, kind=KIND_SUB)
    stacks = device.download_frames(tmp_path, kind=KIND_STACK)

    assert len(subs) >= 1
    assert len(stacks) == 1
    for d in subs:
        sidecar = _read_sidecar(d.sidecar)
        assert sidecar["kind"] == "sub"
        assert sidecar["exposure"] == 10.0
        assert sidecar["gain"] == 80
        assert sidecar["target"] == {"ra": 3.0, "dec": 4.0}
        _assert_valid_fits(d.path, sidecar)
    stack_sidecar = _read_sidecar(stacks[0].sidecar)
    assert stack_sidecar["kind"] == "stack"
    assert stack_sidecar["target"] == {"ra": 3.0, "dec": 4.0}
    _assert_valid_fits(stacks[0].path, stack_sidecar)


def test_redownload_replaces_files_without_leftovers(device, mock_scope, tmp_path):
    mock_scope.add_frame(KIND_SUB, exposure=10.0, gain=80)
    first = device.download_frames(tmp_path, kind=KIND_SUB)
    second = device.download_frames(tmp_path, kind=KIND_SUB)
    assert [d.path for d in first] == [d.path for d in second]
    assert len(list(tmp_path.glob("*.fits"))) == 1
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert not list(tmp_path.glob("*.tmp"))
    _assert_valid_fits(second[0].path, _read_sidecar(second[0].sidecar))


# -- fw 9.75 HTTP fetch: timeout forwarding --------------------------------------


def _http_record() -> dict:
    """A fw 9.75 album record (carries ``remote_path`` -> HTTP download)."""
    return {
        "id": "NGC 6888_sub/Light_NGC 6888_10s_20260101-000000.fit",
        "kind": KIND_SUB,
        "time": "2026-01-01T00:00:00.000",
        "epoch": 1767225600.0,
        "remote_path": "MyWorks/NGC 6888_sub/Light_NGC 6888_10s_20260101-000000.fit",
    }


def _spy_fetch(seen: list[tuple[str, float]]) -> Callable[[str, float], bytes]:
    """An injected HTTP GET recording ``(url, timeout)`` and serving FITS."""

    def spy(url: str, timeout: float) -> bytes:
        seen.append((url, timeout))
        return make_fits_bytes(
            when=datetime(2026, 1, 1, tzinfo=UTC),
            exposure=10.0,
            gain=80,
            target={"name": "NGC 6888"},
        )

    return spy


def test_fetch_frame_bytes_http_forwards_caller_timeout(transport):
    record = _http_record()
    seen: list[tuple[str, float]] = []
    blob = frames.fetch_frame_bytes(
        transport, record, fetch=_spy_fetch(seen), timeout=7.5
    )
    assert blob.startswith(b"SIMPLE")
    assert seen == [(frames.http_frame_url(transport.host, record["remote_path"]), 7.5)]


def test_fetch_frame_bytes_http_default_timeout_when_none(transport):
    """No ``timeout`` given: the HTTP GET still gets the documented 30 s
    default (:data:`frames.DEFAULT_FETCH_TIMEOUT`), not "block forever"."""
    seen: list[tuple[str, float]] = []
    frames.fetch_frame_bytes(transport, _http_record(), fetch=_spy_fetch(seen))
    assert seen[0][1] == frames.DEFAULT_FETCH_TIMEOUT == 30.0


def test_fetch_frame_bytes_http_uses_default_fetch_with_timeout(
    transport, monkeypatch
):
    """No ``fetch`` injected: the HTTP branch calls ``default_fetch``, which
    hands the caller's timeout to ``urlopen`` (spied at that boundary)."""
    calls: list[tuple[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self) -> bytes:
            return b"SIMPLE  = fake fits bytes"

    def fake_urlopen(url, timeout=None):
        calls.append((url, timeout))
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    blob = frames.fetch_frame_bytes(transport, _http_record(), timeout=2.5)
    assert blob == b"SIMPLE  = fake fits bytes"
    assert calls[0][1] == 2.5
    frames.fetch_frame_bytes(transport, _http_record())
    assert calls[1][1] == frames.DEFAULT_FETCH_TIMEOUT


def test_default_fetch_passes_timeout_to_urlopen(monkeypatch):
    calls: list[tuple[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self) -> bytes:
            return b"fits-bytes"

    def fake_urlopen(url, timeout=None):
        calls.append((url, timeout))
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    url = "http://127.0.0.1/MyWorks/NGC 6888_sub/x.fit"
    assert frames.default_fetch(url, timeout=3.5) == b"fits-bytes"
    assert calls == [(url, 3.5)]
    assert frames.default_fetch(url) == b"fits-bytes"
    assert calls[-1] == (url, frames.DEFAULT_FETCH_TIMEOUT)


def test_download_frames_http_path_forwards_timeout(transport, tmp_path, monkeypatch):
    """End to end through ``download_frames`` on the fw 9.75 album flow: the
    injected HTTP GET receives the caller's ``timeout`` and the frame lands
    on disk with its sidecar."""
    monkeypatch.setattr(
        frames, "list_remote_frames", lambda *a, **k: ("get_albums", [_http_record()])
    )
    seen: list[tuple[str, float]] = []
    downloaded = frames.download_frames(
        transport, tmp_path, kind=KIND_SUB, timeout=12.5, fetch=_spy_fetch(seen)
    )
    assert seen[0][1] == 12.5
    assert len(downloaded) == 1
    assert downloaded[0].path.exists() and downloaded[0].sidecar.exists()
    sidecar = _read_sidecar(downloaded[0].sidecar)
    assert sidecar["exposure"] == 10.0 and sidecar["gain"] == 80
    assert sidecar["target"] == {"name": "NGC 6888"}


def test_download_frames_http_path_default_timeout(transport, tmp_path, monkeypatch):
    monkeypatch.setattr(
        frames, "list_remote_frames", lambda *a, **k: ("get_albums", [_http_record()])
    )
    seen: list[tuple[str, float]] = []
    frames.download_frames(transport, tmp_path, kind=KIND_SUB, fetch=_spy_fetch(seen))
    assert seen[0][1] == frames.DEFAULT_FETCH_TIMEOUT


def test_device_download_frames_forwards_timeout_to_http_fetch(
    device, tmp_path, monkeypatch
):
    """The facade forwards both ``timeout`` and ``fetch`` to the HTTP path."""
    record = _http_record()
    monkeypatch.setattr(
        frames, "list_remote_frames", lambda *a, **k: ("get_albums", [record])
    )
    seen: list[tuple[str, float]] = []
    downloaded = device.download_frames(
        tmp_path, kind=KIND_SUB, timeout=9.0, fetch=_spy_fetch(seen)
    )
    expected_url = frames.http_frame_url(device.transport.host, record["remote_path"])
    assert seen == [(expected_url, 9.0)]
    assert len(downloaded) == 1


# -- atomic writes (unit) --------------------------------------------------------


def test_atomic_write_bytes_roundtrip(tmp_path):
    dest = tmp_path / "a.fits"
    out = frames.atomic_write_bytes(dest, b"\x00\x01SIMPLE")
    assert out == dest
    assert dest.read_bytes() == b"\x00\x01SIMPLE"
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_failure_keeps_original_and_cleans_temp(tmp_path, monkeypatch):
    """A failing rename leaves the destination untouched and no temp file."""
    dest = tmp_path / "a.fits"
    dest.write_bytes(b"original")

    def boom(*args, **kwargs):
        raise OSError("rename failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        frames.atomic_write_bytes(dest, b"new-data")
    assert dest.read_bytes() == b"original"  # no partial file
    assert not list(tmp_path.glob("*.tmp"))  # temp cleaned up


def test_atomic_write_interrupt_leaves_no_partial_file(tmp_path, monkeypatch):
    """Simulated interrupt mid-write: no destination, no temp file left."""
    dest = tmp_path / "a.fits"

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "fsync", interrupt)
    with pytest.raises(KeyboardInterrupt):
        frames.atomic_write_bytes(dest, b"partial-data")
    assert not dest.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_json(tmp_path, monkeypatch):
    dest = tmp_path / "a.json"
    frames.atomic_write_json(dest, {"b": 1, "a": [1, 2]})
    assert json.loads(dest.read_text()) == {"a": [1, 2], "b": 1}
    assert not list(tmp_path.glob("*.tmp"))

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "fsync", interrupt)
    with pytest.raises(KeyboardInterrupt):
        frames.atomic_write_json(dest, {"c": 3})
    assert json.loads(dest.read_text()) == {"a": [1, 2], "b": 1}  # untouched
    assert not list(tmp_path.glob("*.tmp"))


# -- helpers (unit) ---------------------------------------------------------------


def test_normalize_since_variants():
    assert frames.normalize_since(None) is None
    dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert frames.normalize_since(dt) == dt.timestamp()
    assert frames.normalize_since(datetime(2026, 1, 1, 12, 0, 0)) == dt.timestamp()  # noqa: DTZ001
    assert frames.normalize_since("2026-01-01T12:00:00+00:00") == dt.timestamp()
    assert frames.normalize_since("2026-01-01T12:00:00Z") == dt.timestamp()
    assert frames.normalize_since(dt.timestamp()) == dt.timestamp()
    assert frames.normalize_since(1767268800) == 1767268800.0
    with pytest.raises(ValueError):
        frames.normalize_since("not a date")
    with pytest.raises(ValueError):
        frames.normalize_since([1, 2])
    with pytest.raises(ValueError):
        frames.normalize_since(True)


def test_normalize_kind():
    assert frames.normalize_kind("sub") == "sub"
    assert frames.normalize_kind("stack") == "stack"
    for bad in ("bogus", "", None, 0):
        with pytest.raises(ValueError):
            frames.normalize_kind(bad)


def test_safe_frame_name():
    assert frames.safe_frame_name("sub-0001") == "sub-0001"
    assert frames.safe_frame_name("a/b\\c d") == "a_b_c_d"
    assert frames.safe_frame_name("/") == "_"  # separators sanitized, not fatal
    for bad in ("..", ".", ""):
        with pytest.raises(ValueError):
            frames.safe_frame_name(bad)


def test_fits_b64_roundtrip():
    blob = b"SIMPLE  = ... fake fits bytes \x00\xff"
    assert frames.decode_fits_b64(frames.encode_fits_b64(blob)) == blob
    with pytest.raises(ValueError):
        frames.decode_fits_b64("!!! not base64 !!!")
    with pytest.raises(ValueError):
        frames.decode_fits_b64(b"bytes-not-str")


def test_parse_frame_meta_and_sidecar():
    meta = frames.parse_frame_meta(
        {
            "id": "sub-0001",
            "kind": "sub",
            "time": "2026-01-01T00:00:00.000",
            "epoch": 1767225600.0,
            "exposure": 10.0,
            "gain": 80,
            "target": {"ra": 1, "dec": 2},
            "size": 2880,
        }
    )
    assert meta == FrameMeta(
        id="sub-0001",
        kind="sub",
        time="2026-01-01T00:00:00.000",
        epoch=1767225600.0,
        exposure=10.0,
        gain=80,
        target={"ra": 1.0, "dec": 2.0},
    )
    sidecar = frames.meta_to_sidecar(meta, size=2880)
    assert sidecar["time"] == meta.time
    assert sidecar["exposure"] == 10.0
    assert sidecar["gain"] == 80
    assert sidecar["target"] == {"ra": 1.0, "dec": 2.0}
    assert sidecar["size"] == 2880
    assert "downloaded_at" in sidecar
    with pytest.raises(ValueError):
        frames.parse_frame_meta({"kind": "sub"})  # no id
    with pytest.raises(ValueError):
        frames.parse_frame_meta({"id": "x", "kind": "bogus"})


def test_make_fits_bytes_is_valid_fits_with_date_obs():
    when = datetime(2026, 5, 19, 22, 30, 15, 123000, tzinfo=UTC)
    blob = make_fits_bytes(
        when=when,
        exposure=10.0,
        gain=80,
        target={"ra": 5.5, "dec": 12.25, "name": "V* test"},
        seed=7,
    )
    assert blob.startswith(b"SIMPLE")
    with pyfits.open(io.BytesIO(blob)) as hdul:
        header = hdul[0].header
        assert str(header["DATE-OBS"]) == "2026-05-19T22:30:15.123"
        assert float(header["EXPTIME"]) == 10.0
        assert int(header["GAIN"]) == 80
        assert float(header["RA"]) == 5.5
        assert float(header["DEC"]) == 12.25
        assert hdul[0].data.shape == (16, 16)
