"""Coordinate utilities: parsing, J2000 normalization, angular distance.

Known coordinate pairs only — no network, no mocks needed (pure math).
M 42 (Orion Nebula): RA 05h35m17.3s = 83.8220833 deg, Dec -05d23m28s =
-5.3911111 deg (the same constants tests/test_catalog_names.py mocks SIMBAD
with). M 43: RA 83.90875 deg, Dec -5.26778 deg.
"""

import dataclasses
import subprocess
import sys

import pytest

from seestar_controller.catalog import (
    CatalogCandidate,
    CoordinateError,
    J2000Coord,
    angular_distance,
    parse_coords,
    parse_dec,
    parse_ra,
)

#: M 42: RA 05h35m17.3s, Dec -05d23m28s (J2000, degrees).
M42 = J2000Coord.from_degrees(83.82208, -5.39114)
M42_SEXA = ("05h35m17.3s", "-05d23m28s")
#: M 43 (from the mocked SIMBAD rows in test_catalog_names.py).
M43 = J2000Coord.from_degrees(83.90875, -5.26778)
#: Haversine distance M 42 <-> M 43, computed independently of the module.
M42_M43_DIST_DEG = 0.15054757032836985


# --- RA parsing: sexagesimal ---------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["12h34m56.7s", "12:34:56.7", "12 34 56.7", "12h34m56.7S", " 12h 34m 56.7s "],
)
def test_parse_ra_sexagesimal_forms_agree(text):
    # 12h34m56.7s = (12 + 34/60 + 56.7/3600) h * 15 deg/h = 188.73625 deg
    assert parse_ra(text) == pytest.approx(188.73625, abs=1e-12)


def test_parse_ra_two_component_forms():
    assert parse_ra("12:34") == pytest.approx((12 + 34 / 60) * 15, abs=1e-12)
    assert parse_ra("12h34m") == pytest.approx((12 + 34 / 60) * 15, abs=1e-12)
    assert parse_ra("12 34") == pytest.approx((12 + 34 / 60) * 15, abs=1e-12)
    assert parse_ra("12h") == pytest.approx(180.0)


def test_parse_ra_m42_sexagesimal():
    assert parse_ra(M42_SEXA[0]) == pytest.approx(83.82208333333332, abs=1e-12)


# --- RA parsing: decimal (hours) and wraparound ---------------------------------


def test_parse_ra_decimal_is_hours():
    # Convention: decimal RA is in HOURS (the unit link.Device.goto takes).
    assert parse_ra("12.5") == pytest.approx(187.5)
    assert parse_ra(12.5) == pytest.approx(187.5)  # numbers are decimal hours too
    assert parse_ra("5.5881388888888885") == pytest.approx(83.82208333333332, abs=1e-9)


def test_parse_ra_wraps_at_24h():
    assert parse_ra("24h00m00s") == pytest.approx(0.0, abs=1e-12)
    assert parse_ra("24h") == pytest.approx(0.0, abs=1e-12)
    assert parse_ra("25h30m") == pytest.approx(22.5)  # 25.5 h -> 1.5 h
    assert parse_ra("24.5") == pytest.approx(7.5)  # 24.5 h -> 0.5 h
    assert parse_ra("0h0m0s") == pytest.approx(0.0)


# --- Dec parsing ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["-05d23m28s", "-05:23:28", "-5 23 28", "-05°23m28s", "-05°23′28″", "-05deg23m28s"],
)
def test_parse_dec_negative_sexagesimal_forms_agree(text):
    # -(5 + 23/60 + 28/3600) deg = -5.3911111 deg
    assert parse_dec(text) == pytest.approx(-5.391111111111112, abs=1e-12)


def test_parse_dec_positive_and_decimal():
    assert parse_dec("5d23m28s") == pytest.approx(5.391111111111112, abs=1e-12)
    assert parse_dec("+45.5") == pytest.approx(45.5)
    assert parse_dec("-5.39114") == pytest.approx(-5.39114)  # decimal Dec is DEGREES
    assert parse_dec(-5.39114) == pytest.approx(-5.39114)
    assert parse_dec("90") == pytest.approx(90.0)
    assert parse_dec("-90") == pytest.approx(-90.0)
    assert parse_dec("0") == pytest.approx(0.0)


# --- invalid input rejected, naming the offending value --------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "abc",
        "12h34m56.7x",
        "12h56s",  # seconds without minutes
        "12h70m",  # minutes >= 60
        "12:34:60",  # seconds >= 60
        "12:34:56:78",  # too many components
        "1 2 3 4",  # too many components
        "12:-34:56",  # sign mid-value
        "-1h",  # RA carries no sign
        "-1",
        "+2:30:00",
        "12,5",
    ],
)
def test_parse_ra_rejects_invalid_input(bad):
    with pytest.raises(ValueError) as excinfo:  # CoordinateError is a ValueError
        parse_ra(bad)
    assert isinstance(excinfo.value, CoordinateError)
    assert repr(bad) in str(excinfo.value) or bad in str(excinfo.value)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "abc",
        "91",  # dec out of range
        "-90.0001",
        "12d70m",  # arcminutes >= 60
        "12:34:60",
        "12:-34:56",
        "1 2 3 4",
        "12h34m",  # RA units are not Dec units
        None,
        [1, 2],
    ],
)
def test_parse_dec_rejects_invalid_input(bad):
    with pytest.raises(ValueError) as excinfo:
        parse_dec(bad)
    assert isinstance(excinfo.value, CoordinateError)
    assert repr(bad) in str(excinfo.value)


def test_parse_ra_rejects_non_string_non_number():
    with pytest.raises(ValueError, match="string or number"):
        parse_ra(None)
    with pytest.raises(ValueError, match="string or number"):
        parse_ra([12, 34])


# --- J2000Coord: the single normalization place ---------------------------------


def test_j2000coord_from_degrees_and_hours():
    c = J2000Coord.from_degrees(83.82208, -5.39114)
    assert c.ra_deg == pytest.approx(83.82208)
    assert c.dec_deg == pytest.approx(-5.39114)
    assert c.ra_hours == pytest.approx(83.82208 / 15.0)
    assert c.as_degrees() == (c.ra_deg, c.dec_deg)
    h = J2000Coord.from_hours(5.5881388888888885, -5.391111111111112)
    assert h.ra_deg == pytest.approx(83.82208333333332, abs=1e-9)
    assert h.dec_deg == pytest.approx(-5.391111111111112, abs=1e-12)


def test_j2000coord_wraps_ra_at_360():
    assert J2000Coord.from_degrees(360.0, 0.0).ra_deg == pytest.approx(0.0)
    assert J2000Coord.from_degrees(-15.0, 0.0).ra_deg == pytest.approx(345.0)
    assert J2000Coord.from_degrees(725.0, 10.0).ra_deg == pytest.approx(5.0)
    assert J2000Coord.from_hours(24.5, 0.0).ra_deg == pytest.approx(7.5)
    assert 0.0 <= J2000Coord.from_degrees(359.999, 0.0).ra_deg < 360.0


@pytest.mark.parametrize("bad_dec", [90.5, -90.0001, -91, float("nan"), float("inf")])
def test_j2000coord_rejects_dec_out_of_range(bad_dec):
    with pytest.raises(ValueError) as excinfo:
        J2000Coord.from_degrees(10.0, bad_dec)
    assert isinstance(excinfo.value, CoordinateError)
    assert repr(bad_dec) in str(excinfo.value)


def test_j2000coord_is_frozen():
    c = J2000Coord.from_degrees(1.0, 2.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.ra_deg = 5.0  # ty: ignore[invalid-assignment]


def test_j2000coord_parse_funnels_through_parsers():
    c = J2000Coord.parse(*M42_SEXA)
    assert c.ra_deg == pytest.approx(83.82208333333332, abs=1e-12)
    assert c.dec_deg == pytest.approx(-5.391111111111112, abs=1e-12)
    d = parse_coords("5.5881388888888885", "-5.391111111111112")
    assert d.ra_deg == pytest.approx(c.ra_deg, abs=1e-9)
    assert d.dec_deg == pytest.approx(c.dec_deg, abs=1e-12)
    with pytest.raises(ValueError, match="91"):
        parse_coords("5.5", "91")


# --- angular distance: known values ----------------------------------------------


def test_angular_distance_same_point_is_zero():
    assert angular_distance(M42, M42) == pytest.approx(0.0, abs=1e-12)
    assert M42.separation(M42) == pytest.approx(0.0, abs=1e-12)


def test_angular_distance_antipodes_are_180():
    assert angular_distance((0.0, 0.0), (180.0, 0.0)) == pytest.approx(180.0, abs=1e-9)
    assert angular_distance((10.0, 20.0), (190.0, -20.0)) == pytest.approx(180.0, abs=1e-9)


def test_angular_distance_pole_and_equator():
    assert angular_distance((0.0, 0.0), (0.0, 90.0)) == pytest.approx(90.0, abs=1e-9)
    assert angular_distance((0.0, 0.0), (90.0, 0.0)) == pytest.approx(90.0, abs=1e-9)
    assert angular_distance((123.0, -30.0), (123.0, 60.0)) == pytest.approx(90.0, abs=1e-9)


def test_angular_distance_ra_wraparound_at_0():
    # (359 deg, 0) vs (1 deg, 0) is 2 deg across the 0/360 seam, not 358 deg.
    assert angular_distance((359.0, 0.0), (1.0, 0.0)) == pytest.approx(2.0, abs=1e-9)
    # 1 deg of RA at dec 45 deg: exact great-circle value 0.70710229 deg
    # (spherical law of cosines; the flat-sky cos(45 deg) = 0.70710678 is only
    # a first-order approximation).
    assert angular_distance((359.5, 45.0), (0.5, 45.0)) == pytest.approx(
        0.7071022936882725, abs=1e-9
    )
    # 23h59m vs 0h01m is 2 minutes of RA = 0.5 deg at the equator.
    a = J2000Coord.parse("23h59m00s", "0d00m00s")
    b = J2000Coord.parse("0h01m00s", "0d00m00s")
    assert angular_distance(a, b) == pytest.approx(0.5, abs=1e-9)


def test_angular_distance_known_pair_m42_vs_m43():
    assert angular_distance(M42, M43) == pytest.approx(M42_M43_DIST_DEG, abs=1e-9)
    assert M42.separation(M43) == pytest.approx(M42_M43_DIST_DEG, abs=1e-9)


def test_angular_distance_is_symmetric():
    pairs = [(M42, M43), ((359.0, 0.0), (1.0, 0.0)), ((10.0, 20.0), (190.0, -20.0))]
    for a, b in pairs:
        assert angular_distance(a, b) == pytest.approx(angular_distance(b, a), abs=1e-12)


def test_angular_distance_accepts_pairs_and_catalog_candidates():
    # (ra_deg, dec_deg) tuples and objects with ra_deg/dec_deg both work.
    assert angular_distance((359.0, 0.0), (1.0, 0.0)) == pytest.approx(2.0, abs=1e-9)
    candidate = CatalogCandidate(name="M 42", ra_deg=83.82208, dec_deg=-5.39114)
    assert angular_distance(candidate, M42) == pytest.approx(0.0, abs=1e-12)
    assert angular_distance(candidate, M43) == pytest.approx(M42_M43_DIST_DEG, abs=1e-9)
    with pytest.raises(ValueError, match="ra_deg"):
        angular_distance("M 42", M42)


# --- simbad reuses the shared utilities ------------------------------------------


def test_catalog_candidate_exposes_j2000coord():
    candidate = CatalogCandidate(name="M 42", ra_deg=83.82208, dec_deg=-5.39114)
    assert candidate.coord == J2000Coord.from_degrees(83.82208, -5.39114)
    assert candidate.ra_hours == candidate.coord.ra_hours
    assert candidate.coords_j2000 == candidate.coord.as_degrees()


# --- module boundary --------------------------------------------------------------


def test_catalog_package_imports_no_gtk_and_no_link():
    code = (
        "import sys;"
        "import seestar_controller.catalog;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert not any(m == 'seestar_controller.link' or m.startswith("
        "'seestar_controller.link.') for m in sys.modules), 'link imported';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_public_api_is_exported_from_package():
    from seestar_controller import catalog

    for name in catalog.__all__:
        assert hasattr(catalog, name), name
    assert issubclass(catalog.CoordinateError, ValueError)
    assert callable(catalog.parse_ra)
    assert callable(catalog.parse_dec)
    assert callable(catalog.parse_coords)
    assert callable(catalog.angular_distance)
