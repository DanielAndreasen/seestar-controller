"""Tests for moving-objects issue 02 — the MPC lookup (``moving_objects/mpc.py``).

NO NETWORK: every test monkeypatches the single HTTP boundary
:func:`~seestar_controller.moving_objects.mpc.fetch_mpc_checker_json` (or
passes a fake), so no test ever touches the network. The disk cache is a
tmp_path :class:`~seestar_controller.catalog.cache.QueryCache`.

Covered: the mover's sky-track params (mid position / rate / PA, RA-wrap),
the checker form fields, the cache key rounding, response parsing (aliases,
malformed payloads), the pure matcher tolerances, the raw + cached lookups
(fresh cache = no HTTP, stale fallback on fetch failure, unavailable with an
empty cache, corrupt cache refetched), the no-sky-mover behavior, and the
persist path (``lookup_and_persist`` writes schema_version 2 with
``mpc_match``).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
from test_moving_objects import (
    T0,
    detect_in_subs,
    flag_linear_movers,
    frozen,
    link_tracks,
    make_session_with_subs,
    save_tracks,
)

from seestar_controller.catalog.cache import QueryCache
from seestar_controller.moving_objects import (
    MoverReport,
    MpcCandidate,
    MpcLookupError,
    MpcMatch,
    MpcResponseError,
    MpcUnavailableError,
    build_checker_params,
    lookup_and_persist,
    lookup_mover,
    lookup_mover_cached,
    lookup_movers,
    match_mover,
    mover_track_params,
    parse_mpc_response,
    with_matches,
)
from seestar_controller.moving_objects import mpc as mpc_module
from seestar_controller.moving_objects.mpc import mpc_cache_key
from seestar_controller.store import create_project

# A synthetic mover with a sky track: 0.01 deg east along dec=-5.391 over
# 3.5 minutes -> 0.01*cos(dec) deg = ~35.84 arcsec over 3.5 min, PA = 90 deg.
START_JD = 2461000.0  # 2025-11-20 12:00:00 UTC
DURATION_MIN = 3.5
END_JD = START_JD + DURATION_MIN / 1440.0
START_RA = 83.633
START_DEC = -5.391
END_RA = START_RA + 0.01
EXPECTED_MOTION = 0.01 * math.cos(math.radians(START_DEC)) * 3600.0 / DURATION_MIN


def make_mover(**overrides) -> MoverReport:
    """A synthetic flagged mover with sky coordinates (WCS present)."""
    fields: dict[str, Any] = {
        "track_id": 0,
        "n_frames": 8,
        "start_utc": "2026-11-20T22:00:00+00:00",
        "end_utc": "2026-11-20T22:03:30+00:00",
        "start_jd": START_JD,
        "end_jd": END_JD,
        "start_x": 12.0,
        "start_y": 44.0,
        "end_x": 26.0,
        "end_y": 44.0,
        "motion_px": 14.0,
        "speed_px_per_frame": 2.0,
        "direction_deg": 90.0,
        "fit_residual_px": 0.01,
        "speed_arcsec_per_frame": 1.0,
        "start_ra_deg": START_RA,
        "start_dec_deg": START_DEC,
        "end_ra_deg": END_RA,
        "end_dec_deg": START_DEC,
    }
    fields.update(overrides)
    return MoverReport(**fields)


def make_candidate(**overrides) -> MpcCandidate:
    """A candidate at the mover's mid position with the same rate/PA."""
    fields: dict[str, Any] = {
        "designation": "(1234) 2001 AB2",
        "name": "Test Asteroid",
        "ra_deg": (START_RA + END_RA) / 2.0,
        "dec_deg": START_DEC,
        "motion_arcsec_per_min": EXPECTED_MOTION,
        "pa_deg": 90.0,
        "v_mag": 18.2,
    }
    fields.update(overrides)
    return MpcCandidate(**fields)


def checker_payload(*candidates: MpcCandidate) -> dict:
    return {
        "objects": [
            {
                "designation": c.designation,
                "name": c.name,
                "ra": c.ra_deg,
                "dec": c.dec_deg,
                "motion": c.motion_arcsec_per_min,
                "pa": c.pa_deg,
                "v": c.v_mag,
            }
            for c in candidates
        ]
    }


@pytest.fixture()
def cache(tmp_path):
    return QueryCache(cache_dir=tmp_path / "mpc-cache", ttl=7 * 24 * 3600.0)


@pytest.fixture()
def fetch_calls(monkeypatch):
    """Monkeypatch the single HTTP boundary; returns the call list + a setter
    for the payload (or exception) it should return."""
    calls: list[dict] = []
    state: dict = {"payload": {"objects": []}, "exc": None}

    def fake_fetch(params, *, timeout=30.0, url=""):
        calls.append(dict(params))
        if state["exc"] is not None:
            raise state["exc"]
        return state["payload"]

    monkeypatch.setattr(mpc_module, "fetch_mpc_checker_json", fake_fetch)
    return calls, state


# --- mover_track_params ---------------------------------------------------------------


def test_mover_track_params_mid_position_rate_pa():
    track = mover_track_params(make_mover())
    assert track["mid_jd"] == pytest.approx((START_JD + END_JD) / 2.0)
    assert track["ra_deg"] == pytest.approx((START_RA + END_RA) / 2.0)
    assert track["dec_deg"] == pytest.approx(START_DEC)
    # 0.01 deg of RA at dec=-5.391 -> 0.01*cos(dec) deg over 3.5 min
    assert track["motion_arcsec_per_min"] == pytest.approx(EXPECTED_MOTION, rel=1e-6)
    assert track["pa_deg"] == pytest.approx(90.0, abs=0.5)  # due east


def test_mover_track_params_ra_wrap():
    # a track crossing RA 0/360: the mid RA must wrap, not average to ~180
    mover = make_mover(start_ra_deg=359.995, end_ra_deg=0.005)
    track = mover_track_params(mover)
    assert track["ra_deg"] == pytest.approx(0.0, abs=1e-3)


def test_mover_track_params_no_sky_raises():
    mover = make_mover(start_ra_deg=None, start_dec_deg=None, end_ra_deg=None, end_dec_deg=None)
    with pytest.raises(MpcLookupError):
        mover_track_params(mover)


def test_mover_track_params_non_positive_duration_raises():
    mover = make_mover(end_jd=START_JD)
    with pytest.raises(MpcLookupError):
        mover_track_params(mover)


def test_mover_track_params_rejects_non_mover():
    with pytest.raises(MpcLookupError):
        mover_track_params({"track_id": 0})  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


# --- build_checker_params ---------------------------------------------------------------


def test_build_checker_params_fields():
    params = build_checker_params(make_mover(), obscode="G37")
    assert params["obscode"] == "G37"
    assert params["format"] == "json"
    assert float(params["ra"]) == pytest.approx((START_RA + END_RA) / 2.0, abs=1e-6)
    assert float(params["dec"]) == pytest.approx(START_DEC, abs=1e-6)
    assert float(params["motion"]) == pytest.approx(EXPECTED_MOTION, rel=1e-3)
    assert float(params["pa"]) == pytest.approx(90.0, abs=0.01)
    # the mid time as calendar fields (JD 2461000.0 = 2025-11-20 12:00 UTC,
    # mid of the 3.5 min track = 12:01:45)
    assert (params["year"], params["month"], params["day"]) == ("2025", "11", "20")
    assert params["hour"] == "12"
    assert params["minute"] == "1"
    assert float(params["second"]) == pytest.approx(45.0, abs=0.01)


def test_build_checker_params_empty_obscode_raises():
    with pytest.raises(MpcLookupError):
        build_checker_params(make_mover(), obscode="  ")


# --- cache key rounding -------------------------------------------------------------------


def test_mpc_cache_key_rounds_track_params():
    key_a = mpc_cache_key(START_JD, START_RA, START_DEC, EXPECTED_MOTION, 90.04)
    # same track, tiny differences below the rounding -> same key
    key_b = mpc_cache_key(
        START_JD + 1e-6, START_RA + 1e-5, START_DEC - 1e-5,
        EXPECTED_MOTION + 0.001, 90.04 + 0.004,
    )
    assert key_a == key_b
    # a clearly different track -> different key
    key_c = mpc_cache_key(START_JD, START_RA + 0.01, START_DEC, EXPECTED_MOTION, 90.04)
    assert key_a != key_c
    # the obscode is part of the key
    key_d = mpc_cache_key(START_JD, START_RA, START_DEC, EXPECTED_MOTION, 90.04, obscode="G37")
    assert key_a != key_d
    # the key is a stable JSON string
    assert json.loads(key_a)["obscode"] == "500"


# --- parse_mpc_response ---------------------------------------------------------------------


def test_parse_mpc_response_objects_key():
    candidates = parse_mpc_response(checker_payload(make_candidate()))
    assert len(candidates) == 1
    c = candidates[0]
    assert c.designation == "(1234) 2001 AB2"
    assert c.name == "Test Asteroid"
    assert c.ra_deg == pytest.approx((START_RA + END_RA) / 2.0)
    assert c.motion_arcsec_per_min == pytest.approx(EXPECTED_MOTION)
    assert c.pa_deg == pytest.approx(90.0)
    assert c.v_mag == pytest.approx(18.2)


def test_parse_mpc_response_empty_is_normal():
    assert parse_mpc_response({"objects": []}) == []


def test_parse_mpc_response_aliases():
    payload = {
        "results": [
            {
                "desig": "2024 AA",
                "object": "Named",
                "RAJ2000": "10.0",
                "DEJ2000": "-5.0",
                "rate": "5.5",
                "posang": "270",
                "mag": "",
            }
        ]
    }
    (c,) = parse_mpc_response(payload)
    assert c.designation == "2024 AA"
    assert c.name == "Named"
    assert c.ra_deg == pytest.approx(10.0)
    assert c.dec_deg == pytest.approx(-5.0)
    assert c.motion_arcsec_per_min == pytest.approx(5.5)
    assert c.pa_deg == pytest.approx(270.0)
    assert c.v_mag is None  # empty string -> None
    # the "data" alias works too
    assert parse_mpc_response({"data": []}) == []


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "a", "dict"],
        {"no_objects_key": True},
        {"objects": {"not": "a list"}},
        {"objects": ["not-a-dict"]},
        {"objects": [{"ra": 10.0, "dec": 0.0, "motion": 1.0, "pa": 0.0}]},  # no designation
        {"objects": [{"designation": "x", "ra": 370.0, "dec": 0.0, "motion": 1.0, "pa": 0.0}]},
        {"objects": [{"designation": "x", "ra": 10.0, "dec": -95.0, "motion": 1.0, "pa": 0.0}]},
        {"objects": [{"designation": "x", "ra": 10.0, "dec": 0.0, "motion": "fast", "pa": 0.0}]},
        {"objects": [{"designation": "x", "ra": 10.0, "dec": 0.0, "motion": -1.0, "pa": 0.0}]},
    ],
)
def test_parse_mpc_response_malformed_raises(payload):
    with pytest.raises(MpcResponseError):
        parse_mpc_response(payload)


# --- match_mover (pure) ----------------------------------------------------------------------


def test_match_mover_within_tolerance():
    match = match_mover(make_mover(), [make_candidate()])
    assert isinstance(match, MpcMatch)
    assert match.designation == "(1234) 2001 AB2"
    assert match.name == "Test Asteroid"
    assert match.offset_arcsec == pytest.approx(0.0, abs=1.0)
    assert match.motion_arcsec_per_min == pytest.approx(EXPECTED_MOTION)
    assert match.pa_deg == pytest.approx(90.0)
    assert match.v_mag == pytest.approx(18.2)


def test_match_mover_offset_too_large():
    # 0.01 deg away = 36 arcsec > the 30 arcsec default tolerance
    far = make_candidate(ra_deg=(START_RA + END_RA) / 2.0 + 0.01)
    assert match_mover(make_mover(), [far]) is None


def test_match_mover_motion_outside_relative_tolerance():
    # 40% off the observed rate > the 25% default
    fast = make_candidate(motion_arcsec_per_min=EXPECTED_MOTION * 1.4)
    assert match_mover(make_mover(), [fast]) is None


def test_match_mover_pa_outside_tolerance():
    wrong_way = make_candidate(pa_deg=90.0 + 30.0)
    assert match_mover(make_mover(), [wrong_way]) is None


def test_match_mover_picks_best_by_offset():
    near = make_candidate(designation="NEAR", ra_deg=(START_RA + END_RA) / 2.0 + 0.0001)
    far = make_candidate(designation="FAR", ra_deg=(START_RA + END_RA) / 2.0 + 0.005)
    match = match_mover(make_mover(), [far, near])
    assert match is not None
    assert match.designation == "NEAR"


def test_match_mover_no_candidates():
    assert match_mover(make_mover(), []) is None


def test_match_mover_no_sky_raises():
    mover = make_mover(start_ra_deg=None, start_dec_deg=None, end_ra_deg=None, end_dec_deg=None)
    with pytest.raises(MpcLookupError):
        match_mover(mover, [make_candidate()])


def test_match_mover_bad_tolerance_raises():
    with pytest.raises(MpcLookupError):
        match_mover(make_mover(), [], max_offset_arcsec=-1.0)


# --- lookup_mover (raw, mocked HTTP boundary) -------------------------------------------------


def test_lookup_mover_fetches_parses_matches(fetch_calls):
    calls, state = fetch_calls
    state["payload"] = checker_payload(make_candidate())
    match = lookup_mover(make_mover())
    assert match is not None and match.designation == "(1234) 2001 AB2"
    assert len(calls) == 1
    assert calls[0]["format"] == "json"
    assert calls[0]["obscode"] == "500"


def test_lookup_mover_no_match_is_none(fetch_calls):
    _, state = fetch_calls
    state["payload"] = {"objects": []}
    assert lookup_mover(make_mover()) is None


def test_lookup_mover_network_failure_raises(fetch_calls):
    _, state = fetch_calls
    state["exc"] = MpcUnavailableError("offline")
    with pytest.raises(MpcUnavailableError):
        lookup_mover(make_mover())


def test_lookup_mover_no_sky_raises_without_http(fetch_calls):
    calls, _ = fetch_calls
    mover = make_mover(start_ra_deg=None, start_dec_deg=None, end_ra_deg=None, end_dec_deg=None)
    with pytest.raises(MpcLookupError):
        lookup_mover(mover)
    assert calls == []  # no HTTP for a pixel-only mover


# --- lookup_mover_cached ------------------------------------------------------------------------


def test_cached_lookup_fetches_once_then_serves_from_disk(fetch_calls, cache):
    calls, state = fetch_calls
    state["payload"] = checker_payload(make_candidate())
    mover = make_mover()
    first = lookup_mover_cached(mover, cache)
    assert first is not None and first.designation == "(1234) 2001 AB2"
    assert len(calls) == 1
    # second lookup: fresh cache entry -> NO http at all
    second = lookup_mover_cached(mover, cache)
    assert second == first
    assert len(calls) == 1
    # the cache dir holds exactly one entry
    assert len(list(Path(cache.cache_dir).glob("*.json"))) == 1


def test_cached_lookup_rounded_key_shares_entry(fetch_calls, cache):
    calls, state = fetch_calls
    state["payload"] = checker_payload(make_candidate())
    lookup_mover_cached(make_mover(), cache)
    # a slightly different track (below every key rounding) reuses the entry
    other = make_mover(end_ra_deg=END_RA + 1e-6)
    lookup_mover_cached(other, cache)
    assert len(calls) == 1


def test_cached_lookup_stale_fallback_on_fetch_failure(monkeypatch, cache):
    # seed the cache with an EXPIRED but valid entry
    mover = make_mover()
    track = mover_track_params(mover)
    key = mpc_cache_key(
        track["mid_jd"], track["ra_deg"], track["dec_deg"],
        track["motion_arcsec_per_min"], track["pa_deg"],
    )
    rows = [
        {
            "designation": "(1234) 2001 AB2",
            "name": "Test Asteroid",
            "ra_deg": (START_RA + END_RA) / 2.0,
            "dec_deg": START_DEC,
            "motion_arcsec_per_min": EXPECTED_MOTION,
            "pa_deg": 90.0,
            "v_mag": 18.2,
        }
    ]
    seed_cache = QueryCache(cache_dir=cache.cache_dir, ttl=1.0, now=lambda: 1_000_000.0)
    seed_cache.set(key, rows)  # fetched_at = 1_000_000, ttl 1s
    expired_cache = QueryCache(
        cache_dir=cache.cache_dir, ttl=1.0, now=lambda: 1_000_000.0 + 100.0
    )
    found = expired_cache.get_any(key)
    assert found is not None and found[1] is False  # expired, but still on disk
    # (do NOT call get() here: it discards expired entries from disk)

    def failing_fetch(params, *, timeout=30.0, url=""):
        raise MpcUnavailableError("offline")

    monkeypatch.setattr(mpc_module, "fetch_mpc_checker_json", failing_fetch)
    match = lookup_mover_cached(mover, expired_cache)
    assert match is not None and match.designation == "(1234) 2001 AB2"


def test_cached_lookup_unavailable_with_empty_cache(fetch_calls, cache):
    _, state = fetch_calls
    state["exc"] = MpcUnavailableError("offline")
    with pytest.raises(MpcUnavailableError):
        lookup_mover_cached(make_mover(), cache)


def test_cached_lookup_malformed_response_not_cached(fetch_calls, cache):
    calls, state = fetch_calls
    state["payload"] = {"objects": [{"designation": "x"}]}  # malformed row
    with pytest.raises(MpcResponseError):
        lookup_mover_cached(make_mover(), cache)
    # nothing was cached; a fixed response is fetched and cached
    state["payload"] = checker_payload(make_candidate())
    match = lookup_mover_cached(make_mover(), cache)
    assert match is not None
    assert len(calls) == 2
    assert len(list(Path(cache.cache_dir).glob("*.json"))) == 1


def test_cached_lookup_corrupt_cache_file_refetched(fetch_calls, cache):
    calls, state = fetch_calls
    state["payload"] = checker_payload(make_candidate())
    mover = make_mover()
    track = mover_track_params(mover)
    key = mpc_cache_key(
        track["mid_jd"], track["ra_deg"], track["dec_deg"],
        track["motion_arcsec_per_min"], track["pa_deg"],
    )
    cache.cache_dir.mkdir(parents=True, exist_ok=True)
    cache.path_for(key).write_text("not json {{{", encoding="utf-8")
    match = lookup_mover_cached(mover, cache)
    assert match is not None and match.designation == "(1234) 2001 AB2"
    assert len(calls) == 1  # the corrupt entry was a miss -> refetched


def test_cached_lookup_wrong_shape_cache_refetched(fetch_calls, cache):
    calls, state = fetch_calls
    state["payload"] = checker_payload(make_candidate())
    mover = make_mover()
    track = mover_track_params(mover)
    key = mpc_cache_key(
        track["mid_jd"], track["ra_deg"], track["dec_deg"],
        track["motion_arcsec_per_min"], track["pa_deg"],
    )
    cache.set(key, {"not": "candidate rows"})  # wrong shape -> treated as a miss
    match = lookup_mover_cached(mover, cache)
    assert match is not None
    assert len(calls) == 1


# --- lookup_movers / with_matches ---------------------------------------------------------------


def test_lookup_movers_no_sky_gets_none(fetch_calls, cache):
    calls, state = fetch_calls
    state["payload"] = checker_payload(make_candidate())
    pixel_only = make_mover(
        track_id=1, start_ra_deg=None, start_dec_deg=None, end_ra_deg=None, end_dec_deg=None
    )
    matches = lookup_movers([make_mover(), pixel_only], cache)
    assert len(matches) == 2
    assert matches[0] is not None and matches[0].designation == "(1234) 2001 AB2"
    assert matches[1] is None  # pixel-only mover: no MPC query possible
    assert len(calls) == 1  # only the sky mover hit the (mocked) HTTP boundary


def test_lookup_movers_rejects_non_mover(cache):
    with pytest.raises(MpcLookupError):
        lookup_movers([{"track_id": 0}], cache)  # type: ignore[list-item]  # ty: ignore[invalid-argument-type]


def test_with_matches_attaches_and_validates_length():
    movers = [make_mover(track_id=0), make_mover(track_id=1)]
    match = MpcMatch(
        designation="X", name=None, offset_arcsec=1.0,
        motion_arcsec_per_min=10.0, pa_deg=90.0, v_mag=None,
    )
    out = with_matches(movers, [match, None])
    assert out[0].mpc_match == match
    assert out[1].mpc_match is None
    assert movers[0].mpc_match is None  # frozen: the originals are unchanged
    with pytest.raises(MpcLookupError):
        with_matches(movers, [match])


# --- lookup_and_persist ---------------------------------------------------------------------------


def test_lookup_and_persist_writes_schema_v2_with_match(tmp_path, monkeypatch):
    project = create_project("M 42", root=tmp_path / "projects")
    try:
        sid = make_session_with_subs(project, wcs=True, n=6)
        frames = detect_in_subs(project, sid)
        tracks = link_tracks(frames, max_motion_px=5.0)
        movers = flag_linear_movers(tracks)
        assert len(movers) == 1
        save_tracks(project, sid, tracks, movers)

        # mock the HTTP boundary: one candidate exactly at the mover's track
        mover = movers[0]
        track = mover_track_params(mover)

        def fake_fetch(params, *, timeout=30.0, url=""):
            return checker_payload(
                MpcCandidate(
                    designation="(99999) 2026 XX",
                    name="Mock Planet",
                    ra_deg=track["ra_deg"],
                    dec_deg=track["dec_deg"],
                    motion_arcsec_per_min=track["motion_arcsec_per_min"],
                    pa_deg=track["pa_deg"],
                    v_mag=17.0,
                )
            )

        monkeypatch.setattr(mpc_module, "fetch_mpc_checker_json", fake_fetch)
        cache = QueryCache(cache_dir=tmp_path / "mpc-cache", ttl=3600.0)
        result = lookup_and_persist(project, sid, cache=cache)
        assert len(result.movers) == 1
        match = result.movers[0].mpc_match
        assert match is not None
        assert match.designation == "(99999) 2026 XX"
        assert match.name == "Mock Planet"

        # persisted: the sidecar is schema_version 2 with the mpc_match
        from seestar_controller.moving_objects import load_tracks, tracks_path

        payload = json.loads(tracks_path(project, sid).read_text(encoding="utf-8"))
        assert payload["schema_version"] == 2
        assert payload["movers"][0]["mpc_match"]["designation"] == "(99999) 2026 XX"
        reloaded = load_tracks(project, sid)
        assert reloaded.movers[0].mpc_match == match
    finally:
        project.close()


def test_lookup_and_persist_without_sidecar_raises(tmp_path):
    from seestar_controller.moving_objects import PersistenceError
    from seestar_controller.store import start_session

    project = create_project("M 42", root=tmp_path / "projects")
    try:
        sid = start_session(project, clock=frozen(T0))
        with pytest.raises(PersistenceError):
            lookup_and_persist(project, sid, cache=QueryCache(cache_dir=tmp_path / "c"))
    finally:
        project.close()
