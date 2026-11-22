"""Tests for seestar_controller.gaia.matching — sky-position pairing."""

from __future__ import annotations

import math
import random
import time
from typing import Any, cast

import pytest

from seestar_controller.catalog.coords import angular_distance
from seestar_controller.gaia import matching as gc
from seestar_controller.gaia.detect import DetectedSource
from seestar_controller.gaia.query import GaiaStar

ARCSEC = 1.0 / 3600.0  # one arcsecond in degrees


def det(x: float, y: float, ra: float, dec: float, flux: float = 1000.0) -> DetectedSource:
    return DetectedSource(x=x, y=y, ra_deg=ra, dec_deg=dec, peak=flux, flux=flux)


def star(source_id: int, ra: float, dec: float, g: float = 15.0) -> GaiaStar:
    return GaiaStar(
        source_id=source_id,
        ra_deg=ra,
        dec_deg=dec,
        g=g,
        bp=g + 0.5,
        rp=g - 0.5,
        parallax=1.25,
        parallax_error=0.05,
        pmra=2.5,
        pmra_error=0.06,
        pmdec=-3.5,
        pmdec_error=0.07,
    )


RA0, DEC0 = 83.633, 22.014


def test_crossmatch_pairs_detections_with_their_gaia_rows():
    detected = [det(10.0, 20.0, RA0, DEC0), det(50.0, 60.0, RA0 + 0.01, DEC0 + 0.01)]
    gaia = [
        star(111, RA0 + 0.5 * ARCSEC, DEC0, g=14.0),
        star(222, RA0 + 0.01 + 0.2 * ARCSEC, DEC0 + 0.01, g=18.0),
    ]
    rows = gc.crossmatch(detected, gaia, radius_arcsec=1.5)
    assert len(rows) == 2
    first, second = rows
    assert first.matched and first.source_id == 111
    assert first.g == pytest.approx(14.0)
    assert first.bp == pytest.approx(14.5)
    assert first.rp == pytest.approx(13.5)
    assert first.parallax == pytest.approx(1.25)
    assert first.parallax_error == pytest.approx(0.05)
    assert first.pmra == pytest.approx(2.5)
    assert first.pmra_error == pytest.approx(0.06)
    assert first.pmdec == pytest.approx(-3.5)
    assert first.pmdec_error == pytest.approx(0.07)
    assert first.separation_arcsec == pytest.approx(0.5, abs=0.05)
    assert first.x == 10.0 and first.y == 20.0
    assert first.ra_deg == pytest.approx(RA0) and first.dec_deg == pytest.approx(DEC0)
    assert second.matched and second.source_id == 222
    assert second.separation_arcsec == pytest.approx(0.2, abs=0.05)


def test_crossmatch_unmatched_detections_are_flagged():
    detected = [det(1.0, 2.0, RA0, DEC0), det(3.0, 4.0, RA0 + 1.0, DEC0)]  # 1 deg away
    gaia = [star(111, RA0, DEC0)]
    rows = gc.crossmatch(detected, gaia, radius_arcsec=1.5)
    assert rows[0].matched and rows[0].source_id == 111
    unmatched = rows[1]
    assert not unmatched.matched
    assert unmatched.source_id is None
    assert unmatched.g is None and unmatched.bp is None and unmatched.rp is None
    assert unmatched.parallax is None and unmatched.parallax_error is None
    assert unmatched.pmra is None and unmatched.pmra_error is None
    assert unmatched.pmdec is None and unmatched.pmdec_error is None
    assert unmatched.separation_arcsec is None
    # the detection's own position is still reported
    assert unmatched.x == 3.0 and unmatched.y == 4.0
    assert unmatched.ra_deg == pytest.approx(RA0 + 1.0)


def test_crossmatch_match_radius_is_configurable():
    detected = [det(0.0, 0.0, RA0, DEC0)]
    gaia = [star(111, RA0, DEC0 + 1.0 * ARCSEC)]  # exactly 1 arcsec away
    assert gc.crossmatch(detected, gaia, radius_arcsec=0.5)[0].matched is False
    assert gc.crossmatch(detected, gaia, radius_arcsec=1.5)[0].matched is True
    assert gc.crossmatch(detected, gaia, radius_arcsec=3.0)[0].separation_arcsec == pytest.approx(1.0, abs=0.05)
    # default radius is 1.5 arcsec
    assert gc.DEFAULT_MATCH_RADIUS_ARCSEC == 1.5
    assert gc.crossmatch(detected, gaia)[0].matched is True
    gaia_far = [star(111, RA0, DEC0 + 2.0 * ARCSEC)]
    assert gc.crossmatch(detected, gaia_far)[0].matched is False


def test_crossmatch_is_one_to_one_nearest_wins():
    # two detections near one Gaia row: only the closer one is matched
    detected = [det(0.0, 0.0, RA0, DEC0), det(1.0, 1.0, RA0 + 0.5 * ARCSEC, DEC0)]
    gaia = [star(111, RA0 + 0.1 * ARCSEC, DEC0)]
    rows = gc.crossmatch(detected, gaia, radius_arcsec=1.5)
    assert rows[0].matched and rows[0].source_id == 111  # 0.1 arcsec away
    assert not rows[1].matched  # 0.4 arcsec away, but the Gaia row is taken
    # with a bigger radius both could match only if two Gaia rows exist
    gaia2 = [star(111, RA0 + 0.1 * ARCSEC, DEC0), star(222, RA0 + 0.6 * ARCSEC, DEC0)]
    rows2 = gc.crossmatch(detected, gaia2, radius_arcsec=1.5)
    assert rows2[0].source_id == 111
    assert rows2[1].source_id == 222


def test_crossmatch_preserves_detection_order_and_handles_empty_inputs():
    detected = [det(9.0, 9.0, RA0 + 0.02, DEC0), det(1.0, 1.0, RA0, DEC0)]
    gaia = [star(111, RA0, DEC0)]
    rows = gc.crossmatch(detected, gaia)
    assert [r.x for r in rows] == [9.0, 1.0]
    assert rows[1].matched and not rows[0].matched
    assert gc.crossmatch([], gaia) == []
    assert gc.crossmatch(detected, []) == [
        gc.CrossmatchRow(x=9.0, y=9.0, ra_deg=RA0 + 0.02, dec_deg=DEC0, matched=False),
        gc.CrossmatchRow(x=1.0, y=1.0, ra_deg=RA0, dec_deg=DEC0, matched=False),
    ]


def test_crossmatch_ra_wraparound():
    detected = [det(0.0, 0.0, 359.9999, 10.0)]
    gaia = [star(111, 0.0001, 10.0)]  # ~0.72 arcsec across the wrap
    rows = gc.crossmatch(detected, gaia, radius_arcsec=1.5)
    assert rows[0].matched
    assert rows[0].separation_arcsec == pytest.approx(0.72, abs=0.1)


def test_crossmatch_validates_radius_and_inputs():
    detected = [det(0.0, 0.0, RA0, DEC0)]
    gaia = [star(111, RA0, DEC0)]
    for bad in cast(Any, (0.0, -1.0, float("nan"), float("inf"), "1.5")):
        with pytest.raises(ValueError):
            gc.crossmatch(detected, gaia, radius_arcsec=bad)
    with pytest.raises(ValueError):
        gc.crossmatch([object()], gaia)
    with pytest.raises(ValueError):
        gc.crossmatch(detected, [object()])
    with pytest.raises(ValueError):
        gc.crossmatch([det(0.0, 0.0, float("nan"), DEC0)], gaia)


def test_crossmatch_accepts_duck_typed_rows():
    class D:
        x, y, ra_deg, dec_deg = 1.0, 2.0, RA0, DEC0

    class G:
        ra_deg, dec_deg, source_id, g = RA0, DEC0, 5, 10.0

    rows = gc.crossmatch([D()], [G()])
    assert rows[0].matched and rows[0].source_id == 5 and rows[0].g == 10.0
    assert rows[0].bp is None  # missing attribute -> None
    # the duck-typed Gaia row has no error attributes either -> None
    assert rows[0].parallax is None and rows[0].parallax_error is None
    assert rows[0].pmra_error is None and rows[0].pmdec_error is None
    sep = rows[0].separation_arcsec
    assert sep is not None and math.isfinite(sep)


# --- equivalence with the brute-force loop + scale -----------------------------


def _brute_force(
    detected: list[DetectedSource], gaia: list[GaiaStar], radius_arcsec: float
) -> list[gc.CrossmatchRow]:
    """The O(detections × rows) reference: evaluate every pair with the exact
    distance, then the same greedy one-to-one assignment."""
    radius = float(radius_arcsec)
    candidates: list[tuple[float, int, int]] = []
    for i, d in enumerate(detected):
        ra_d, dec_d = d.ra_deg % 360.0, d.dec_deg
        for j, s in enumerate(gaia):
            sep = angular_distance((ra_d, dec_d), (s.ra_deg % 360.0, s.dec_deg)) * 3600.0
            if sep <= radius:
                candidates.append((sep, i, j))
    candidates.sort()
    matched_i: dict[int, tuple[float, int]] = {}
    used_j: set[int] = set()
    for sep, i, j in candidates:
        if i in matched_i or j in used_j:
            continue
        matched_i[i] = (sep, j)
        used_j.add(j)
    rows: list[gc.CrossmatchRow] = []
    for i, d in enumerate(detected):
        ra_d, dec_d = d.ra_deg % 360.0, d.dec_deg
        if i in matched_i:
            sep, j = matched_i[i]
            s = gaia[j]
            rows.append(
                gc.CrossmatchRow(
                    x=float(d.x), y=float(d.y), ra_deg=ra_d, dec_deg=dec_d,
                    matched=True, source_id=s.source_id, g=s.g, bp=s.bp, rp=s.rp,
                    parallax=s.parallax, parallax_error=s.parallax_error,
                    pmra=s.pmra, pmra_error=s.pmra_error,
                    pmdec=s.pmdec, pmdec_error=s.pmdec_error,
                    separation_arcsec=sep,
                )
            )
        else:
            rows.append(
                gc.CrossmatchRow(x=float(d.x), y=float(d.y), ra_deg=ra_d, dec_deg=dec_d, matched=False)
            )
    return rows


def test_crossmatch_is_identical_to_the_brute_force_loop_on_random_fields():
    rng = random.Random(20260621)
    # (ra0, dec0, field half-width in deg, radius_arcsec) — ordinary field,
    # across the RA 0/360 wrap, near the pole, and small/large radii
    configs = [
        (83.633, 22.014, 0.02, 1.5),
        (359.98, 10.0, 0.04, 1.5),
        (0.0, 89.98, 180.0, 5.0),  # near the pole, RA spread over the full circle
        (200.0, -30.0, 0.03, 0.4),
        (200.0, -30.0, 0.03, 30.0),
    ]
    for ra0, dec0, width, radius in configs:
        stars = [
            star(1000 + k, (ra0 + rng.uniform(-width, width)) % 360.0, dec0 + rng.uniform(-width, width))
            for k in range(400)
        ]
        dets = [
            det(rng.uniform(0, 100), rng.uniform(0, 100),
                (ra0 + rng.uniform(-width, width)) % 360.0, dec0 + rng.uniform(-width, width))
            for _ in range(60)
        ]
        # cluster some detections on top of stars to force one-to-one contention
        for _ in range(15):
            s = stars[rng.randrange(len(stars))]
            dets.append(
                det(rng.uniform(0, 100), rng.uniform(0, 100),
                    (s.ra_deg + rng.uniform(-0.4, 0.4) * ARCSEC) % 360.0,
                    s.dec_deg + rng.uniform(-0.4, 0.4) * ARCSEC)
            )
        assert gc.crossmatch(dets, stars, radius_arcsec=radius) == _brute_force(dets, stars, radius)


def test_crossmatch_scale_2000_detections_x_100k_gaia_rows_is_fast():
    # documented defaults: a dense Seestar field at DEFAULT_MAXREC=100_000 rows
    rng = random.Random(20260622)
    n_star = 100_000
    stars = [star(k, 83.0 + rng.random(), 22.0 + rng.random()) for k in range(n_star)]
    dets = []
    for k in rng.sample(range(n_star), 2000):
        s = stars[k]
        dets.append(det(float(k), float(k), s.ra_deg % 360.0, s.dec_deg + 0.2 * ARCSEC))
    start = time.perf_counter()
    rows = gc.crossmatch(dets, stars, radius_arcsec=1.5)
    elapsed = time.perf_counter() - start
    # generous bound: the brute-force detection×Gaia loop needs minutes here
    assert elapsed < 10.0, f"crossmatch(2000 x 100k) took {elapsed:.1f} s"
    assert len(rows) == 2000
    matched = [r for r in rows if r.matched]
    assert len(matched) >= 1900  # each detection sits 0.2 arcsec from its own star
    # (a handful may pair with an even closer neighbouring Gaia row instead)
    own = [r for r in matched if r.separation_arcsec == pytest.approx(0.2, abs=0.05)]
    assert len(own) >= 1900
