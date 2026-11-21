"""hr-diagram ticket 02 — the PURE PARSEC isochrone overlay module.

Covers :mod:`seestar_controller.hr_diagram.isochrone`: parameter validation,
the PARSEC wire parse, the disk cache (keyed by (age, feh), atomic writes,
never-raising reads), the single HTTP boundary :func:`fetch_isochrone_json`
(monkeypatched — NO test ever touches the network), the bundled fallback
grid, the ``isochrone()`` resolution order (cache -> fetch -> fallback), and
the no-gi / no-store / offline subprocess boundary.
"""

from __future__ import annotations

import importlib
import json
import math
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import seestar_controller.hr_diagram as hr

#: The isochrone MODULE (the package attribute ``hr_diagram.isochrone`` is the
#: function of the same name, so import the module via importlib).
iso = importlib.import_module("seestar_controller.hr_diagram.isochrone")
from seestar_controller.hr_diagram.isochrone import (
    CACHE_DIR_ENV_VAR,
    DEFAULT_CACHE_DIR,
    DEFAULT_TIMEOUT_S,
    FALLBACK_FILE,
    KEY_AGE_DIGITS,
    KEY_FEH_DIGITS,
    MAX_AGE_LOG,
    MAX_DIST_MOD,
    MAX_FEH,
    MIN_AGE_LOG,
    MIN_DIST_MOD,
    MIN_FEH,
    PARSEC_ISOCHRONE_URL,
    IsochroneError,
    IsochroneFetchError,
    IsochronePoint,
    build_isochrone_params,
    cache_file,
    cache_key,
    check_isochrone_params,
    default_cache_dir,
    fallback_isochrone,
    fetch_isochrone_json,
    isochrone,
    load_fallback_grid,
    parse_isochrone_payload,
    read_cached_points,
    write_cached_points,
)

ISO_PACKAGE_DIR = Path(iso.__file__).resolve().parent

#: A small synthetic PARSEC-like payload: rows with the Gaia columns (the
#: magnitudes are dyadic so BP - RP subtracts exactly in binary floats).
PAYLOAD_ROWS = [
    {"Gmag": 5.0, "G_BPmag": 1.25, "G_RPmag": 0.25},
    {"Gmag": 8.5, "G_BPmag": 2.5, "G_RPmag": 1.0},
    {"Gmag": 12.25, "G_BPmag": 4.5, "G_RPmag": 2.0},
]
#: The parsed raw pairs of PAYLOAD_ROWS: (bp_rp = BP - RP, abs_g = Gmag).
PAYLOAD_PAIRS = ((1.0, 5.0), (1.5, 8.5), (2.5, 12.25))


def _fetcher_returning(payload, calls: list | None = None):
    """A fetcher stub recording its calls and returning ``payload``."""

    def fetch(url, params, *, timeout):
        if calls is not None:
            calls.append({"url": url, "params": params, "timeout": timeout})
        return payload

    return fetch


def _raising_fetcher(exc: BaseException | None = None):
    def fetch(url, params, *, timeout):
        raise exc if exc is not None else RuntimeError("network!")

    return fetch


# --- parameter validation -------------------------------------------------------------------


def test_check_isochrone_params_accepts_boundaries_and_returns_floats():
    assert check_isochrone_params(6, -4, -5) == (6.0, -4.0, -5.0)
    assert check_isochrone_params(10.5, 0.5, 30.0) == (10.5, 0.5, 30.0)
    assert check_isochrone_params(8, 0, 10) == (8.0, 0.0, 10.0)
    assert MIN_AGE_LOG == 6.0 and MAX_AGE_LOG == 10.5
    assert MIN_FEH == -4.0 and MAX_FEH == 0.5
    assert MIN_DIST_MOD == -5.0 and MAX_DIST_MOD == 30.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"age": 5.9}, {"age": 10.6}, {"age": float("nan")}, {"age": float("inf")},
        {"feh": -4.1}, {"feh": 0.6}, {"feh": float("-inf")},
        {"dist_mod": -5.1}, {"dist_mod": 30.1}, {"dist_mod": float("nan")},
    ],
)
def test_check_isochrone_params_rejects_bad_values(kwargs):
    good = {"age": 8.0, "feh": 0.0, "dist_mod": 10.0}
    good.update(kwargs)
    with pytest.raises(IsochroneError) as excinfo:
        check_isochrone_params(**good)
    assert issubclass(IsochroneError, ValueError)
    assert next(iter(kwargs)) in str(excinfo.value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"age": "8.0"}, {"age": True}, {"age": None}, {"age": [8.0]},
        {"feh": "0.0"}, {"feh": False}, {"dist_mod": "10"}, {"dist_mod": {}},
    ],
)
def test_check_isochrone_params_rejects_non_numbers_with_type_error(kwargs):
    good = {"age": 8.0, "feh": 0.0, "dist_mod": 10.0}
    good.update(kwargs)
    with pytest.raises(TypeError):
        check_isochrone_params(**good)


def test_isochrone_validates_before_any_io(tmp_path):
    """Validation errors raise even with a fetcher that would succeed and a
    cache that would hit — nothing is read or fetched for bad parameters."""
    calls: list = []
    with pytest.raises(IsochroneError):
        isochrone(5.9, 0.0, 10.0, cache_dir=tmp_path, fetcher=_fetcher_returning(PAYLOAD_ROWS, calls))
    with pytest.raises(TypeError):
        isochrone("8", 0.0, 10.0, cache_dir=tmp_path, fetcher=_fetcher_returning(PAYLOAD_ROWS, calls))
    assert calls == []
    assert list(tmp_path.iterdir()) == []  # no cache dir created either


def test_build_isochrone_params_form_fields():
    params = build_isochrone_params(8.0, -0.5)
    assert params == {"logAge": "8.00", "FeH": "-0.50", "FORMAT": "json"}
    assert build_isochrone_params(9.876, 0.123)["logAge"] == f"{9.88:.{KEY_AGE_DIGITS}f}"
    with pytest.raises(IsochroneError):
        build_isochrone_params(11.0, 0.0)
    with pytest.raises(TypeError):
        build_isochrone_params(8.0, "x")  # type: ignore[arg-type]


# --- the PARSEC wire parse -------------------------------------------------------------------


def test_parse_isochrone_payload_list_and_rows_wrapper():
    pairs = parse_isochrone_payload(PAYLOAD_ROWS)
    assert pairs == PAYLOAD_PAIRS  # bp_rp = BP - RP, abs_g = Gmag
    assert parse_isochrone_payload({"rows": PAYLOAD_ROWS}) == pairs
    # ints are fine too (numbers.Real)
    assert parse_isochrone_payload([{"Gmag": 5, "G_BPmag": 2, "G_RPmag": 1}]) == ((1.0, 5.0),)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        42,
        "nope",
        {},
        {"rows": []},
        [],
        [{"Gmag": 5.0, "G_BPmag": 1.0}],  # missing G_RPmag
        [{"Gmag": 5.0, "G_BPmag": "x", "G_RPmag": 1.0}],  # non-numeric
        [{"Gmag": 5.0, "G_BPmag": True, "G_RPmag": 1.0}],  # bool is not a number here
        [{"Gmag": float("nan"), "G_BPmag": 1.0, "G_RPmag": 1.0}],  # non-finite
        [{"Gmag": float("inf"), "G_BPmag": 1.0, "G_RPmag": 1.0}],
        ["not-a-dict"],
        [None],
    ],
)
def test_parse_isochrone_payload_rejects_bad_shapes(payload):
    with pytest.raises(IsochroneError):
        parse_isochrone_payload(payload)


# --- the disk cache --------------------------------------------------------------------------


def test_cache_key_rounds_to_the_key_digits():
    assert cache_key(8.001, -0.501) == cache_key(8.004, -0.504)  # same 2-decimal bucket
    assert cache_key(8.006, 0.0) != cache_key(8.0, 0.0)
    assert cache_key(8.0, 0.0) != cache_key(8.0, -0.5)
    # a rounded -0.0 shares the "0.00" bucket with 0.0 (no "-0.00" split)
    assert cache_key(8.0, -0.004) == cache_key(8.0, 0.0)
    assert cache_key(8.0, -0.0) == cache_key(8.0, 0.0)
    assert cache_key(8.0, -0.006) != cache_key(8.0, 0.0)  # rounds to -0.01
    assert KEY_AGE_DIGITS == 2 and KEY_FEH_DIGITS == 2
    assert cache_file("/tmp/c", 8.0, 0.0) == Path("/tmp/c") / (cache_key(8.0, 0.0) + ".json")


def test_default_cache_dir_env_var_precedence(monkeypatch, tmp_path):
    monkeypatch.delenv(CACHE_DIR_ENV_VAR, raising=False)
    assert default_cache_dir() == DEFAULT_CACHE_DIR
    assert default_cache_dir({}) == DEFAULT_CACHE_DIR
    assert default_cache_dir({CACHE_DIR_ENV_VAR: "/x/y"}) == Path("/x/y")
    monkeypatch.setenv(CACHE_DIR_ENV_VAR, str(tmp_path / "env-cache"))
    assert default_cache_dir() == tmp_path / "env-cache"


def test_cache_write_then_read_round_trip_and_is_atomic(tmp_path):
    pairs = ((0.8, 5.0), (1.0, 8.5))
    path = write_cached_points(tmp_path, 8.0, 0.0, pairs)
    assert path == cache_file(tmp_path, 8.0, 0.0)
    assert path is not None and path.exists()
    assert read_cached_points(tmp_path, 8.0, 0.0) == pairs
    # the entry records the rounded parameters and a fetch time
    entry = json.loads(path.read_text(encoding="utf-8"))
    assert entry["age"] == 8.0 and entry["feh"] == 0.0
    assert isinstance(entry["fetched_at"], float)
    assert entry["points"] == [[0.8, 5.0], [1.0, 8.5]]
    # atomic write: no temp files left behind
    assert not list(tmp_path.glob("*.tmp"))
    # a nearby (age, feh) in the same key bucket hits the same entry
    assert read_cached_points(tmp_path, 8.001, -0.004) == pairs


def test_read_cached_points_never_raises_on_missing_or_corrupt(tmp_path):
    assert read_cached_points(tmp_path, 8.0, 0.0) is None  # missing
    path = cache_file(tmp_path, 8.0, 0.0)
    path.write_text("not json at all", encoding="utf-8")
    assert read_cached_points(tmp_path, 8.0, 0.0) is None  # corrupt JSON
    for bad in [
        '{"points": "nope"}',
        '{"points": [[1.0]]}',
        '{"points": [[1.0, "x"]]}',
        '{"points": [[1.0, true]]}',
        '{"points": [[1.0, NaN]]}'.replace("NaN", '"nan"'),
        '{"points": [[1.0, 2.0], [3.0, "Infinity"]]}',
        '[]',
        '42',
        '{"no_points": []}',
    ]:
        path.write_text(bad, encoding="utf-8")
        assert read_cached_points(tmp_path, 8.0, 0.0) is None, bad
    # an unreadable location is a miss too
    assert read_cached_points(tmp_path / "missing-dir", 8.0, 0.0) is None


def test_write_cached_points_is_best_effort(tmp_path):
    # the parent "dir" is a FILE -> mkdir fails -> None, never raises
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    assert write_cached_points(blocker / "cache", 8.0, 0.0, [(0.8, 5.0)]) is None


# --- the single HTTP boundary (monkeypatched urlopen — offline) ------------------------------


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_fetch_isochrone_json_posts_the_form_and_parses_json(monkeypatch):
    seen: list = []

    def fake_urlopen(request, timeout):
        seen.append((request, timeout))
        return _FakeResponse(json.dumps(PAYLOAD_ROWS).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    payload = fetch_isochrone_json(
        url=PARSEC_ISOCHRONE_URL, params={"logAge": "8.00"}, timeout=7.5
    )
    assert payload == PAYLOAD_ROWS
    request, timeout = seen[0]
    assert timeout == 7.5
    assert request.full_url == PARSEC_ISOCHRONE_URL
    assert request.get_method() == "POST"
    assert b"logAge=8.00" in request.data
    assert request.headers["Content-type"] == "application/x-www-form-urlencoded"


def test_fetch_isochrone_json_failures_raise_fetch_error(monkeypatch):
    def raise_url_error(request, timeout):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(urllib.request, "urlopen", raise_url_error)
    with pytest.raises(IsochroneFetchError):
        fetch_isochrone_json()

    def raise_http_error(request, timeout):
        raise urllib.error.HTTPError(PARSEC_ISOCHRONE_URL, 500, "boom", {}, None)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(urllib.request, "urlopen", raise_http_error)
    with pytest.raises(IsochroneFetchError) as excinfo:
        fetch_isochrone_json()
    assert "500" in str(excinfo.value)

    def raise_timeout(request, timeout):
        raise TimeoutError("slow")

    monkeypatch.setattr(urllib.request, "urlopen", raise_timeout)
    with pytest.raises(IsochroneFetchError):
        fetch_isochrone_json()

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout: _FakeResponse(b"<html>no</html>")
    )
    with pytest.raises(IsochroneFetchError):
        fetch_isochrone_json()

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout: _FakeResponse(b"42")
    )
    with pytest.raises(IsochroneFetchError):
        fetch_isochrone_json()
    assert issubclass(IsochroneFetchError, IsochroneError)
    assert PARSEC_ISOCHRONE_URL.startswith("https://")
    assert DEFAULT_TIMEOUT_S == 30.0


# --- the bundled fallback grid ----------------------------------------------------------------


def test_load_fallback_grid_reads_the_bundled_file():
    grid = load_fallback_grid()
    assert FALLBACK_FILE.exists() and FALLBACK_FILE.suffix == ".json"
    assert len(grid) == 28
    ages = {entry[0] for entry in grid}
    fehs = {entry[1] for entry in grid}
    assert ages == {7.0, 7.5, 8.0, 8.5, 9.0, 9.5, 10.0}
    assert fehs == {0.0, -0.5, -1.0, -1.5}
    for _age, _feh, points in grid:
        assert len(points) == 48
        assert all(
            isinstance(p, tuple) and len(p) == 2 and all(math.isfinite(v) for v in p)
            for p in points
        )


def test_fallback_isochrone_serves_the_nearest_grid_entry():
    # exact grid point -> distance 0
    exact = fallback_isochrone(8.0, 0.0, 10.0)
    grid = { (e[0], e[1]): e[2] for e in load_fallback_grid() }
    assert [(p.bp_rp, p.abs_g) for p in exact] == [tuple(p) for p in grid[(8.0, 0.0)]]
    assert all(p.g == p.abs_g + 10.0 for p in exact)
    # (8.3, -0.2): nearest is (8.5, 0.0) — d2 = .04+.04 = .08 beats (8.0, 0.0) .13
    near = fallback_isochrone(8.3, -0.2, 0.0)
    assert [(p.bp_rp, p.abs_g) for p in near] == [tuple(p) for p in grid[(8.5, 0.0)]]
    assert all(p.g == p.abs_g for p in near)
    # dist_mod is applied on return
    shifted = fallback_isochrone(8.0, 0.0, 12.5)
    assert all(p.g == p.abs_g + 12.5 for p in shifted)
    # validation still applies
    with pytest.raises(IsochroneError):
        fallback_isochrone(11.0, 0.0, 10.0)
    with pytest.raises(TypeError):
        fallback_isochrone(8.0, "0", 10.0)  # type: ignore[arg-type]


# --- isochrone(): the resolution order cache -> fetch -> fallback -----------------------------


def test_isochrone_fetches_parses_and_caches_on_a_miss(tmp_path):
    calls: list = []
    points = isochrone(
        8.0, 0.0, 10.0, cache_dir=tmp_path, fetcher=_fetcher_returning(PAYLOAD_ROWS, calls)
    )
    assert len(calls) == 1
    assert calls[0]["url"] == PARSEC_ISOCHRONE_URL
    assert calls[0]["params"] == build_isochrone_params(8.0, 0.0)
    assert calls[0]["timeout"] == DEFAULT_TIMEOUT_S
    assert points == (
        IsochronePoint(bp_rp=1.0, abs_g=5.0, g=15.0),
        IsochronePoint(bp_rp=1.5, abs_g=8.5, g=18.5),
        IsochronePoint(bp_rp=2.5, abs_g=12.25, g=22.25),
    )
    assert all(isinstance(p, IsochronePoint) for p in points)
    # the result was cached: a second call does NOT fetch again
    again = isochrone(
        8.0, 0.0, 10.0, cache_dir=tmp_path, fetcher=_raising_fetcher(AssertionError("fetched!"))
    )
    assert again == points
    assert len(calls) == 1


def test_isochrone_cache_hit_serves_without_any_fetch(tmp_path):
    write_cached_points(tmp_path, 9.0, -1.0, [(0.5, 4.0), (1.5, 9.0)])
    points = isochrone(
        9.0, -1.0, 7.5, cache_dir=tmp_path, fetcher=_raising_fetcher(AssertionError("fetched!"))
    )
    assert points == (IsochronePoint(0.5, 4.0, 11.5), IsochronePoint(1.5, 9.0, 16.5))


def test_isochrone_dist_mod_is_not_part_of_the_cache_key(tmp_path):
    calls: list = []
    fetcher = _fetcher_returning(PAYLOAD_ROWS, calls)
    first = isochrone(8.0, 0.0, 10.0, cache_dir=tmp_path, fetcher=fetcher)
    # same (age, feh) bucket, different dist_mod -> cache hit, no fetch, g shifted
    second = isochrone(8.001, -0.004, 20.0, cache_dir=tmp_path, fetcher=fetcher)
    assert len(calls) == 1
    assert [p.bp_rp for p in second] == [p.bp_rp for p in first]
    assert [p.abs_g for p in second] == [p.abs_g for p in first]
    assert all(p.g == p.abs_g + 20.0 for p in second)


def test_isochrone_falls_back_to_the_bundled_grid_on_fetch_failure(tmp_path):
    points = isochrone(
        8.0, 0.0, 10.0, cache_dir=tmp_path, fetcher=_raising_fetcher(RuntimeError("down"))
    )
    assert len(points) == 48  # the bundled grid entry (8.0, 0.0)
    assert all(p.g == p.abs_g + 10.0 for p in points)
    # a parse failure falls back too
    points2 = isochrone(
        8.0, 0.0, 10.0, cache_dir=tmp_path / "other", fetcher=_fetcher_returning({"rows": []})
    )
    assert len(points2) == 48


def test_isochrone_allow_fallback_false_reraises(tmp_path):
    with pytest.raises(RuntimeError, match="down"):
        isochrone(
            8.0, 0.0, 10.0, cache_dir=tmp_path,
            fetcher=_raising_fetcher(RuntimeError("down")), allow_fallback=False,
        )
    with pytest.raises(IsochroneError):
        isochrone(
            8.0, 0.0, 10.0, cache_dir=tmp_path / "other",
            fetcher=_fetcher_returning("garbage"), allow_fallback=False,
        )
    # nothing was cached on the failed path
    assert read_cached_points(tmp_path, 8.0, 0.0) is None


def test_isochrone_use_cache_false_skips_read_and_write(tmp_path):
    calls: list = []
    fetcher = _fetcher_returning(PAYLOAD_ROWS, calls)
    isochrone(8.0, 0.0, 10.0, cache_dir=tmp_path, fetcher=fetcher, use_cache=False)
    isochrone(8.0, 0.0, 10.0, cache_dir=tmp_path, fetcher=fetcher, use_cache=False)
    assert len(calls) == 2  # fetched every time
    assert read_cached_points(tmp_path, 8.0, 0.0) is None  # never written
    # and a pre-existing cache entry is ignored with use_cache=False
    write_cached_points(tmp_path, 8.0, 0.0, [(0.1, 1.0)])
    points = isochrone(8.0, 0.0, 10.0, cache_dir=tmp_path, fetcher=fetcher, use_cache=False)
    assert [(p.bp_rp, p.abs_g) for p in points] == [tuple(p) for p in PAYLOAD_PAIRS]


def test_isochrone_uses_the_module_level_fetch_when_no_fetcher_given(
    monkeypatch, tmp_path
):
    """With ``fetcher=None`` the module-level :func:`fetch_isochrone_json` is
    looked up AT CALL TIME, so monkeypatching it works (the documented test
    seam)."""
    calls: list = []

    def fake_fetch(url, params, *, timeout):
        calls.append((url, params, timeout))
        return PAYLOAD_ROWS

    monkeypatch.setattr(iso, "fetch_isochrone_json", fake_fetch)
    points = isochrone(8.0, 0.0, 10.0, cache_dir=tmp_path)
    assert len(points) == 3 and len(calls) == 1
    assert calls[0][0] == PARSEC_ISOCHRONE_URL
    # and the monkeypatched failure falls back to the bundled grid
    monkeypatch.setattr(iso, "fetch_isochrone_json", _raising_fetcher(RuntimeError("x")))
    assert len(isochrone(8.0, 0.0, 10.0, cache_dir=tmp_path / "c2")) == 48


def test_isochrone_default_cache_dir_comes_from_the_env(monkeypatch, tmp_path):
    monkeypatch.setenv(CACHE_DIR_ENV_VAR, str(tmp_path / "env-cache"))
    points = isochrone(8.0, 0.0, 10.0, fetcher=_fetcher_returning(PAYLOAD_ROWS))
    assert len(points) == 3
    assert cache_file(tmp_path / "env-cache", 8.0, 0.0).exists()


# --- public API + package boundary -------------------------------------------------------------


def test_isochrone_names_are_exported_from_the_package():
    for name in (
        "PARSEC_ISOCHRONE_URL", "IsochroneError", "IsochroneFetchError",
        "IsochronePoint", "fallback_isochrone", "fetch_isochrone_json", "isochrone",
    ):
        assert name in hr.__all__
        assert hasattr(hr, name)


def test_isochrone_module_source_is_pure():
    source = (ISO_PACKAGE_DIR / "isochrone.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE)
    assert not re.search(r"^\s*(import subprocess|from subprocess)\b", source, re.MULTILINE)
    for match in re.finditer(
        r"^\s*(?:from|import)\s+(seestar_controller[\w.]*)", source, re.MULTILINE
    ):
        assert match.group(1).startswith("seestar_controller.hr_diagram"), match.group(1)


def test_isochrone_module_imports_no_gi_no_store_and_works_offline():
    code = """
import socket

class _BlockedSocket:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("network access blocked in test")

socket.socket = _BlockedSocket
socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blocked"))
socket.getaddrinfo = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blocked"))

import sys
import tempfile
from pathlib import Path

import seestar_controller.hr_diagram as hr
from seestar_controller.hr_diagram import isochrone

assert "gi" not in sys.modules, "GTK imported"
assert "seestar_controller.link" not in sys.modules, "link imported"
assert "seestar_controller.ui" not in sys.modules, "ui imported"
# ticket 03 added the store-boundary controller module, imported eagerly by
# the package __init__ (the same role as gaia.overlay) — so the STORE (and
# its transitive imports) now come in with the package; the isochrone module
# itself stays pure (the source scan in test_hr_diagram.py enforces that),
# and the widget module stays lazy:
assert "seestar_controller.hr_diagram.view" not in sys.modules, "widget module imported"

def boom(url, params, *, timeout):
    raise RuntimeError("network must not be touched")

with tempfile.TemporaryDirectory() as td:
    pts = isochrone(8.0, 0.0, 10.0, cache_dir=Path(td) / "c", fetcher=boom)
    assert len(pts) == 48, len(pts)  # bundled fallback, fully offline
    assert all(p.g == p.abs_g + 10.0 for p in pts)
    # cache hit path: no fetch at all
    pts2 = isochrone(8.0, 0.0, 10.0, cache_dir=Path(td) / "c", fetcher=boom)
    assert pts2 == pts

print("clean")
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout
