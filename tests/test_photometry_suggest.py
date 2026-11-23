"""Pure-layer tests for photometry ticket 02 — catalog assist for comps
(:mod:`seestar_controller.photometry.suggest`).

Headless: no ``gi`` anywhere (the sandbox has no GTK, see
docs/wiki/assumptions.md). The catalog is MOCKED: the
:class:`~seestar_controller.photometry.suggest.SuggestionController` takes an
injectable ``lookup`` callable, so no test touches the network or the
catalog's HTTP boundary — the fakes are real
:class:`~seestar_controller.catalog.standards.StandardStar` objects (plus a
duck-typed variant). The WCS helpers are exercised with synthetic FITS headers
built via :class:`astropy.wcs.WCS` (CAR — exactly linear), and the save/load
round-trip with a catalog-added comp runs on a tmp project via
:mod:`seestar_controller.store` — the same pattern as
tests/test_photometry_picking.py.

Covered: radius validation + re-filtering (stars outside the configured
radius are excluded, ``set_radius`` re-searches), sorting — nearest-first by
default plus the issue-07 sort modes (by magnitude, closest in magnitude to
the target pick; client-side re-sort on change, no re-lookup),
malformed catalog entries skipped, catalog failures (e.g.
:class:`~seestar_controller.catalog.facade.CatalogUnavailableError`) surfaced
as a clean snapshot ``error`` (never a traceback) with recovery, adding a
suggestion as a comp pick at the exact ``sky_to_pixel`` pixel (round-trip on
the synthetic WCS), duplicate/out-of-frame/bad-index/no-picker/no-image
errors, following the target pick, the listener pattern (idempotent, isolated,
frozen snapshots), the lazy default catalog facade, and save/load with a
catalog-added comp. Ticket 05: a pixel-only target (frame without a WCS, e.g.
a raw Seestar sub) degrades gracefully — the "plate-solve first" hint in the
snapshot ``error``, no catalog lookup, no exception.

The widget layer (``SuggestionsPanel``) is tested in
tests/test_photometry_widgets.py (skips headless).
"""

import dataclasses
import io
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from astropy.io import fits as pyfits

from seestar_controller import photometry
from seestar_controller.catalog.coords import J2000Coord, angular_distance
from seestar_controller.catalog.facade import CatalogUnavailableError
from seestar_controller.catalog.standards import StandardStar
from seestar_controller.photometry import (
    NO_WCS_HINT,
    SORT_CLOSEST_MAG,
    SORT_DISTANCE,
    SORT_MAGNITUDE,
    SORT_MODES,
    PhotometryError,
    PickController,
    PickingError,
    SuggestionController,
    SuggestionError,
    SuggestionSnapshot,
    has_celestial_wcs,
    pixel_to_sky,
    sky_to_pixel,
)
from seestar_controller.photometry import suggest as suggest_module
from seestar_controller.store import (
    KIND_STACK,
    create_project,
    load_star_picks,
    start_session,
    store_frame,
)

#: Directory of the photometry package (source-scan audit).
PHOTOMETRY_PACKAGE_DIR = Path(photometry.__file__).resolve().parent

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)
DATE1 = "2026-11-20T22:00:00.000"

#: The synthetic frame: 64x64, CAR WCS, CRVAL (83, 22) at 1-based pixel (51, 51).
CRVAL = (83.0, 22.0)


# --- synthetic WCS headers + images (same pattern as test_photometry_picking) -----------


def wcs_header(
    *,
    crval: tuple[float, float] = CRVAL,
    crpix: tuple[float, float] = (51.0, 51.0),
    cdelt: tuple[float, float] = (-0.001, 0.001),
) -> dict:
    """A plain-dict FITS header with a simple CAR WCS (exactly linear)."""
    from astropy.wcs import WCS

    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---CAR", "DEC--CAR"]
    w.wcs.crval = list(crval)
    w.wcs.crpix = list(crpix)
    w.wcs.cdelt = list(cdelt)
    return dict(w.to_header())


class FakeImage:
    """A duck-typed FitsImage stand-in (``data`` / ``header`` / ``width`` /
    ``height``) — the pure layer never imports enhance."""

    def __init__(self, data: np.ndarray, header: dict) -> None:
        self.data = np.asarray(data, dtype=np.float32)
        self.header = header

    @property
    def width(self) -> int:
        return int(self.data.shape[1])

    @property
    def height(self) -> int:
        return int(self.data.shape[0])


def synthetic_image(size: int = 64, seed: int = 1) -> np.ndarray:
    """A synthetic image: flat background + Gaussian star peaks + noise."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    data = np.full((size, size), 10.0, dtype=np.float64)
    for cy, cx, peak in ((30, 40, 8000.0), (16, 16, 5000.0)):
        data = data + peak * np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / 8.0))
    data = data + rng.normal(0.0, 1.0, size=(size, size))
    return np.ascontiguousarray(data, dtype=np.float32)


def fits_bytes(data: np.ndarray, *, wcs: dict | None = None, date_obs: str = DATE1) -> bytes:
    """A small valid FITS file with the given data + WCS header cards."""
    hdu = pyfits.PrimaryHDU(data=data)
    hdu.header["DATE-OBS"] = date_obs
    hdu.header["EXPTIME"] = 10.0
    hdu.header["GAIN"] = 50
    hdu.header["OBJECT"] = "M 42"
    if wcs is not None:
        for key, value in wcs.items():
            hdu.header[key] = value
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def frozen(dt: datetime):
    return lambda: dt


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    p = create_project("M 42 night", root=root)
    yield p
    p.close()


@pytest.fixture()
def wcs_stack_project(project):
    """A tmp project with one session and one stored stack carrying a WCS."""
    session = start_session(project, clock=frozen(T0))
    frame = store_frame(
        project,
        session,
        KIND_STACK,
        fits_bytes(synthetic_image(64), wcs=wcs_header(), date_obs=DATE1),
        meta={"target": "M 42", "exposure": 10.0, "gain": 50},
    )
    return project, frame


# --- fakes + fixtures ----------------------------------------------------------------------


def star(name: str, ra_deg: float, dec_deg: float, mag: float | None = 12.5, band: str = "V"):
    """A real frozen :class:`StandardStar` (``distance_deg`` is recomputed by
    the controller anyway — the catalog fills it, the controller re-derives
    it from the target)."""
    return StandardStar(
        name=name,
        coord=J2000Coord(ra_deg=ra_deg, dec_deg=dec_deg),
        magnitude=mag,
        band=band,
        distance_deg=0.0,
    )


def make_lookup(stars, calls: list | None = None):
    """A mocked catalog callable: records ``(coord, radius_deg)`` calls and
    returns the given stars."""

    def lookup(coord, radius_deg):
        if calls is not None:
            calls.append((coord, radius_deg))
        return list(stars)

    return lookup


@pytest.fixture()
def image():
    return FakeImage(synthetic_image(64), wcs_header())


@pytest.fixture()
def picker(image):
    """A PickController with the synthetic image set and a target picked at
    pixel (40, 30) — sky (83.01, 21.98) on the CAR WCS."""
    ctrl = PickController()
    ctrl.set_image(image)
    ctrl.pick_at(40, 30, "target", label="Target", snap=False)
    return ctrl


@pytest.fixture()
def target_coord(picker):
    return picker.target.coord


#: Standard stars around the target (83.01, 21.98). The 64x64 frame at
#: 0.001 deg/px spans ra 82.987..83.05 / dec 21.987..22.05, so A, B and E are
#: ON-frame (in_frame=True, addable as comps), while C (~2 deg) and D (on the
#: 1.5 deg edge) are off-frame: still listed (radius permitting) but marked
#: in_frame=False (issue 08) and never added.
STAR_A = star("APASS A", 83.01, 22.00, mag=11.2, band="V")  # 0.02 deg away, on-frame
STAR_B = star("APASS B", 83.005, 21.98, mag=13.4, band="R")  # ~0.0046 deg, on-frame
STAR_C = star("APASS C", 83.01, 24.0, mag=9.9, band="V")  # ~2 deg away
STAR_D = star("APASS D", 83.01, 23.48, mag=None, band="B")  # ~1.5 deg away (edge)
STAR_E = star("APASS E", 83.008, 22.005, mag=12.3, band="V")  # ~0.025 deg away, on-frame


# --- error hierarchy + radius validation ----------------------------------------------------


def test_error_hierarchy():
    assert issubclass(SuggestionError, PhotometryError)
    assert issubclass(PhotometryError, ValueError)
    assert issubclass(PickingError, PhotometryError)


@pytest.mark.parametrize("bad", [0, -1, 0.0, 180.5, 181, float("nan"), float("inf"), "1.5", None, True, [1.0]])
def test_radius_validation_rejects_bad_values(bad):
    with pytest.raises(SuggestionError, match="radius_deg"):
        SuggestionController(radius_deg=bad)
    ctrl = SuggestionController(radius_deg=1.5)
    with pytest.raises(SuggestionError, match="radius_deg"):
        ctrl.set_radius(bad)
    assert ctrl.radius_deg == 1.5  # unchanged


@pytest.mark.parametrize("good", [0.001, 1.5, 90, 180, 180.0])
def test_radius_validation_accepts_good_values(good):
    ctrl = SuggestionController(radius_deg=good)
    assert ctrl.radius_deg == float(good)
    assert ctrl.set_radius(2.0) == 2.0
    assert ctrl.radius_deg == 2.0


def test_default_radius_constant():
    # issue 08: the default reflects the S50 FOV (~0.5 deg), not the old 1.5 deg
    assert SuggestionController().radius_deg == suggest_module.DEFAULT_COMP_SEARCH_RADIUS_DEG == 0.5
    assert suggest_module.MAX_COMP_SEARCH_RADIUS_DEG == 180.0


# --- refresh: sorting, filtering, empty states ----------------------------------------------


def test_refresh_without_target_is_empty_and_does_not_call_lookup():
    calls: list = []
    ctrl = SuggestionController(lookup=make_lookup([STAR_A], calls), radius_deg=1.5)
    snap = ctrl.refresh()
    assert snap.target is None and snap.suggestions == () and snap.error is None
    assert calls == []
    # a picker without a target behaves the same
    ctrl.attach_picker(PickController())
    snap = ctrl.refresh()
    assert snap.target is None and snap.suggestions == ()
    assert calls == []


# --- ticket 05: pixel-only target (frame without WCS) -> plate-solve hint, never an error -------


def no_wcs_header() -> dict:
    """A Seestar-style SUB header: NAXIS=2, NO CTYPE cards (no celestial WCS)."""
    return {
        "SIMPLE": True,
        "BITPIX": 16,
        "NAXIS": 2,
        "NAXIS1": 64,
        "NAXIS2": 64,
        "COMMENT": "  FITS (Flexible Image Transport System) format is defined in 'Astronomy\n  and Astrophysics'",
        "RA": 359.47083,
        "DEC": 56.726111,
        "DATE-OBS": "2026-11-20T21:33:33.024310",
        "OBJECT": "M 31",
    }


def test_pixel_only_target_gives_plate_solve_hint_without_lookup_or_exception():
    """A target picked on a frame WITHOUT a WCS (a raw Seestar sub) is
    pixel-only: catalog assist degrades gracefully — the catalog lookup is
    NOT called, nothing raises, and the snapshot carries the clear
    "plate-solve first" hint (ticket 05)."""
    calls: list = []
    picker = PickController()
    picker.set_image(FakeImage(synthetic_image(64), no_wcs_header()))
    image = picker.image
    assert image is not None
    assert not has_celestial_wcs(image.header)
    picker.pick_at(40, 30, "target", label="Target", snap=False)
    assert picker.target is not None and picker.target.coord is None

    ctrl = SuggestionController(picker, lookup=make_lookup([STAR_A], calls), radius_deg=1.5)
    snap = ctrl.snapshot  # the attach already refreshed
    assert snap.target is None and snap.suggestions == ()
    assert snap.error == NO_WCS_HINT and "plate-solve" in snap.error
    assert calls == []  # no catalog lookup without sky coords
    # refresh keeps the hint and never raises
    snap = ctrl.refresh()
    assert snap.error == NO_WCS_HINT and snap.suggestions == () and calls == []
    # adding a suggestion is impossible (no suggestions) — a clean error
    with pytest.raises(SuggestionError, match="out of range"):
        ctrl.add_suggestion(0)
    # a radius change with a pixel-only target keeps the hint, no lookup
    ctrl.set_radius(2.0)
    assert ctrl.snapshot.error == NO_WCS_HINT and calls == []

    # recovery: the same picker on a WCS frame clears the hint and searches
    picker.set_image(FakeImage(synthetic_image(64), wcs_header()))
    picker.pick_at(40, 30, "target", snap=False)
    snap = ctrl.snapshot
    assert snap.error is None and calls and [s.name for s in snap.suggestions] == ["APASS A"]
    assert snap.target == picker.target.coord


def test_pixel_only_target_hint_notifies_listeners_and_clears_on_clear():
    seen: list[SuggestionSnapshot] = []
    picker = PickController()
    picker.set_image(FakeImage(synthetic_image(64), no_wcs_header()))
    ctrl = SuggestionController(picker, lookup=make_lookup([STAR_A]), radius_deg=1.5)
    ctrl.add_listener(seen.append)
    picker.pick_at(40, 30, "target", snap=False)  # auto-triggers the hint state
    assert seen and seen[-1].error == NO_WCS_HINT and seen[-1].suggestions == ()
    # clearing the picks returns to the clean empty state (no hint)
    picker.clear()
    assert ctrl.snapshot.error is None and ctrl.snapshot.target is None


def test_refresh_lists_suggestions_nearest_first_and_filters_by_radius(picker, target_coord):
    calls: list = []
    ctrl = SuggestionController(picker, lookup=make_lookup([STAR_C, STAR_B, STAR_A], calls), radius_deg=1.5)
    snap = ctrl.refresh()
    # the lookup was called with the target coord + the configured radius
    # (the constructor's attach_picker already searched once, refresh again)
    assert calls and all(c == (target_coord, 1.5) for c in calls)
    # STAR_C (~2 deg away) is outside the 1.5 deg radius -> excluded; A and B
    # sorted nearest-first (B is closer: cos(dec) scales the ra offset)
    assert [s.name for s in snap.suggestions] == ["APASS B", "APASS A"]
    assert snap.error is None and snap.target == target_coord and snap.radius_deg == 1.5
    a, b = snap.suggestions[1], snap.suggestions[0]
    assert a.distance_deg == pytest.approx(angular_distance(target_coord, STAR_A.coord), abs=1e-12)
    assert b.distance_deg == pytest.approx(angular_distance(target_coord, STAR_B.coord), abs=1e-12)
    assert b.distance_deg < a.distance_deg
    assert a.magnitude == 11.2 and a.band == "V"
    assert "APASS A" in a.describe() and "11.20" in a.describe() and "0.0200" in a.describe()
    # the snapshot + suggestions are frozen
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.error = "x"  # type: ignore[misc]  # ty: ignore[invalid-assignment]
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.name = "x"  # type: ignore[misc]  # ty: ignore[invalid-assignment]
    assert ctrl.snapshot == snap


def test_refresh_keeps_star_exactly_on_the_radius_edge(picker):
    ctrl = SuggestionController(picker, lookup=make_lookup([STAR_D]), radius_deg=1.5)
    snap = ctrl.refresh()
    # ~1.5 deg away (WCS float noise ~1e-6 deg) — still inside the radius
    assert angular_distance(picker.target.coord, STAR_D.coord) == pytest.approx(1.5, abs=1e-4)
    assert [s.name for s in snap.suggestions] == ["APASS D"]
    assert snap.suggestions[0].magnitude is None
    assert "?" in snap.suggestions[0].describe()  # unknown magnitude rendered as "B ?"


def test_refresh_skips_malformed_entries_and_accepts_duck_typed_stars(picker):
    duck = SimpleNamespace(name="DUCK 1", ra_deg=83.02, dec_deg=21.99, magnitude="12.0", band="V")
    no_coord = SimpleNamespace(name="BROKEN", magnitude=10.0)
    ctrl = SuggestionController(picker, lookup=make_lookup([duck, no_coord, STAR_A]), radius_deg=1.5)
    snap = ctrl.refresh()
    assert [s.name for s in snap.suggestions] == ["DUCK 1", "APASS A"]  # nearest first
    assert snap.suggestions[0].coord == J2000Coord(ra_deg=83.02, dec_deg=21.99)
    assert snap.suggestions[0].magnitude == 12.0  # string magnitude coerced


def test_set_radius_researches_and_respects_new_radius(picker, target_coord):
    calls: list = []
    ctrl = SuggestionController(picker, lookup=make_lookup([STAR_A, STAR_B, STAR_C], calls), radius_deg=1.5)
    ctrl.refresh()
    assert len(ctrl.suggestions) == 2
    # shrink the radius below both stars' distances -> re-searched, now empty
    ctrl.set_radius(0.004)
    assert calls[-1] == (target_coord, 0.004)
    assert ctrl.suggestions == ()
    # grow it past C's distance: C appears now (nearest-first)
    ctrl.set_radius(2.5)
    assert calls[-1] == (target_coord, 2.5)
    assert [s.name for s in ctrl.suggestions] == ["APASS B", "APASS A", "APASS C"]
    # setting the same radius is a no-op (no extra lookup call)
    n = len(calls)
    ctrl.set_radius(2.5)
    assert len(calls) == n


# --- issue 07: sort modes — distance (default) / magnitude / closest-to-target-mag --------------


def test_sort_mode_default_is_distance_and_both_are_validated():
    assert SORT_MODES == (SORT_DISTANCE, SORT_MAGNITUDE, SORT_CLOSEST_MAG) == (
        "distance",
        "magnitude",
        "closest_mag",
    )
    ctrl = SuggestionController()
    assert ctrl.sort_mode == SORT_DISTANCE  # default = the historical behavior
    assert ctrl.snapshot.sort_mode == SORT_DISTANCE  # snapshot carries it too
    for mode in SORT_MODES:
        assert SuggestionController(sort_mode=mode).sort_mode == mode
        ctrl.sort_mode = mode
        assert ctrl.sort_mode == mode and ctrl.snapshot.sort_mode == mode
    # bad modes are rejected by the constructor AND the setter, state unchanged
    for bad in ("nearest", "", "DISTANCE", None, 0, True, ["distance"]):
        with pytest.raises(SuggestionError, match="sort_mode"):
            SuggestionController(sort_mode=bad)  # ty: ignore[invalid-argument-type]
        with pytest.raises(SuggestionError, match="sort_mode"):
            ctrl.sort_mode = bad  # ty: ignore[invalid-assignment]
    assert ctrl.sort_mode == SORT_CLOSEST_MAG


def test_target_magnitude_validation():
    ctrl = SuggestionController()
    assert ctrl.target_magnitude is None  # default: unknown
    for bad in (float("nan"), float("inf"), -float("inf"), "12", True, [11.0]):
        with pytest.raises(SuggestionError, match="target_magnitude"):
            SuggestionController(target_magnitude=bad)  # ty: ignore[invalid-argument-type]
        with pytest.raises(SuggestionError, match="target_magnitude"):
            ctrl.target_magnitude = bad  # ty: ignore[invalid-assignment]
    assert ctrl.target_magnitude is None  # unchanged
    ctrl.target_magnitude = 12.3
    assert ctrl.target_magnitude == 12.3
    ctrl.target_magnitude = 0  # mag 0 is a valid number
    assert ctrl.target_magnitude == 0.0
    ctrl.target_magnitude = None
    assert ctrl.target_magnitude is None


def test_snapshot_sort_mode_field_is_backwards_compatible():
    # positional construction without sort_mode keeps working (default distance)
    snap = SuggestionSnapshot(target=None, radius_deg=1.5, suggestions=(), error=None)
    assert snap.sort_mode == SORT_DISTANCE
    assert SuggestionSnapshot(None, 1.5, (), None, SORT_MAGNITUDE).sort_mode == SORT_MAGNITUDE


def test_refresh_sort_by_magnitude_is_brightest_first_unknown_last(picker):
    ctrl = SuggestionController(
        picker,
        lookup=make_lookup([STAR_B, STAR_E, STAR_A, STAR_C, STAR_D]),
        radius_deg=2.5,
        sort_mode=SORT_MAGNITUDE,
    )
    snap = ctrl.refresh()
    # brightest first: C 9.9, A 11.2, E 12.3, B 13.4; D (mag None) sorts last
    assert [s.name for s in snap.suggestions] == [
        "APASS C",
        "APASS A",
        "APASS E",
        "APASS B",
        "APASS D",
    ]
    assert snap.sort_mode == SORT_MAGNITUDE


def test_sort_by_magnitude_ties_break_by_name(picker):
    zed = star("ZED 1", 83.01, 22.00, mag=12.0)
    alpha = star("ALPHA 2", 83.01, 22.01, mag=12.0)
    ctrl = SuggestionController(
        picker, lookup=make_lookup([zed, alpha]), radius_deg=1.5, sort_mode=SORT_MAGNITUDE
    )
    snap = ctrl.refresh()
    assert [s.name for s in snap.suggestions] == ["ALPHA 2", "ZED 1"]  # name tie-break


def test_refresh_sort_closest_mag_to_target_magnitude(picker):
    ctrl = SuggestionController(
        picker,
        lookup=make_lookup([STAR_A, STAR_B, STAR_C, STAR_E]),
        radius_deg=2.5,
        sort_mode=SORT_CLOSEST_MAG,
        target_magnitude=11.0,
    )
    snap = ctrl.refresh()
    # |mag - 11.0|: A 0.2, C 1.1, E 1.3, B 2.4
    assert [s.name for s in snap.suggestions] == ["APASS A", "APASS C", "APASS E", "APASS B"]


def test_sort_closest_mag_unknown_magnitude_sorts_last(picker):
    ctrl = SuggestionController(
        picker,
        lookup=make_lookup([STAR_D, STAR_B]),
        radius_deg=2.5,
        sort_mode=SORT_CLOSEST_MAG,
        target_magnitude=13.0,
    )
    snap = ctrl.refresh()
    # B |13.4-13| = 0.4 first; D has no magnitude -> always last
    assert [s.name for s in snap.suggestions] == ["APASS B", "APASS D"]


def test_sort_closest_mag_without_target_magnitude_falls_back_to_distance(picker):
    ctrl = SuggestionController(
        picker,
        lookup=make_lookup([STAR_A, STAR_B, STAR_C, STAR_E]),
        radius_deg=2.5,
        sort_mode=SORT_CLOSEST_MAG,  # target_magnitude=None (the default)
    )
    snap = ctrl.refresh()
    # graceful fallback: nearest-first (B 0.0046, A 0.02, E 0.025, C ~2 deg)
    assert [s.name for s in snap.suggestions] == ["APASS B", "APASS A", "APASS E", "APASS C"]


def test_changing_sort_mode_resorts_client_side_without_new_lookup_and_notifies(picker):
    calls: list = []
    seen: list[SuggestionSnapshot] = []
    ctrl = SuggestionController(
        picker, lookup=make_lookup([STAR_A, STAR_B, STAR_C], calls), radius_deg=2.5
    )
    ctrl.add_listener(seen.append)
    ctrl.refresh()
    assert [s.name for s in ctrl.suggestions] == ["APASS B", "APASS A", "APASS C"]  # distance
    n_calls, n_seen = len(calls), len(seen)
    # switching to magnitude re-sorts the SAME suggestions — no new catalog call
    ctrl.sort_mode = SORT_MAGNITUDE
    assert len(calls) == n_calls and len(seen) == n_seen + 1
    assert seen[-1].sort_mode == SORT_MAGNITUDE
    assert [s.name for s in ctrl.suggestions] == ["APASS C", "APASS A", "APASS B"]  # 9.9/11.2/13.4
    # setting the same mode again is a no-op (no notify)
    ctrl.sort_mode = SORT_MAGNITUDE
    assert len(seen) == n_seen + 1
    # setting a target magnitude re-sorts + notifies (order unchanged here)
    ctrl.target_magnitude = 13.0
    assert len(seen) == n_seen + 2 and ctrl.target_magnitude == 13.0
    # closest-mag mode: B |13.4-13|=0.4, A |11.2-13|=1.8, C |9.9-13|=3.1
    ctrl.sort_mode = SORT_CLOSEST_MAG
    assert [s.name for s in ctrl.suggestions] == ["APASS B", "APASS A", "APASS C"]
    # changing the target magnitude re-sorts in closest-mag mode (still no lookup)
    ctrl.target_magnitude = 9.9
    assert len(calls) == n_calls
    assert [s.name for s in ctrl.suggestions] == ["APASS C", "APASS A", "APASS B"]  # 0/1.3/3.5
    # a fresh search also honors the active sort mode
    ctrl.refresh()
    assert [s.name for s in ctrl.suggestions] == ["APASS C", "APASS A", "APASS B"]
    assert ctrl.snapshot.sort_mode == SORT_CLOSEST_MAG


# --- catalog failures -> clean error state ------------------------------------------------------


def test_catalog_unavailable_is_a_clean_error_state_and_recovers(picker):
    def failing(coord, radius_deg):
        raise CatalogUnavailableError("offline and no cache")

    ctrl = SuggestionController(picker, lookup=failing, radius_deg=1.5)
    snap = ctrl.refresh()  # must NOT raise
    assert snap.error is not None and "offline and no cache" in snap.error
    assert snap.suggestions == () and snap.target == picker.target.coord
    assert ctrl.error == snap.error
    # any exception type lands in the error state, never a traceback
    ctrl._lookup = lambda coord, radius: 1 / 0  # ty: ignore[invalid-assignment]
    snap = ctrl.refresh()
    assert snap.error is not None and "division" in snap.error
    # recovery: a working lookup clears the error
    ctrl._lookup = make_lookup([STAR_A])
    snap = ctrl.refresh()
    assert snap.error is None and [s.name for s in snap.suggestions] == ["APASS A"]


def test_refresh_error_notifies_listeners(picker):
    seen: list[SuggestionSnapshot] = []
    ctrl = SuggestionController(picker, lookup=make_lookup([STAR_A]))
    ctrl.add_listener(seen.append)
    ctrl._lookup = lambda coord, radius: (_ for _ in ()).throw(RuntimeError("boom"))
    ctrl.refresh()
    assert seen and seen[-1].error is not None and "boom" in seen[-1].error


# --- adding a suggestion as a comp pick -----------------------------------------------------------


def test_add_suggestion_adds_comp_at_exact_wcs_pixel(picker, image, target_coord):
    ctrl = SuggestionController(picker, lookup=make_lookup([STAR_A, STAR_B]), radius_deg=1.5)
    ctrl.refresh()
    spec = ctrl.add_suggestion(0)  # APASS B (nearest)
    assert spec.role == "comp" and spec.label == "APASS B"
    assert spec.coord == STAR_B.coord  # the GIVEN sky coord is kept
    # the pixel is exactly sky_to_pixel of the suggestion against the frame WCS
    expected_x, expected_y = sky_to_pixel(image.header, STAR_B.coord)
    assert (spec.x, spec.y) == pytest.approx((expected_x, expected_y), abs=1e-9)
    # ... and round-trips back to the same sky position (snap=False: no drift)
    back = pixel_to_sky(image.header, spec.x, spec.y)
    assert back.ra_deg == pytest.approx(STAR_B.coord.ra_deg, abs=1e-9)
    assert back.dec_deg == pytest.approx(STAR_B.coord.dec_deg, abs=1e-9)
    # comps accumulate, the target is untouched
    spec2 = ctrl.add_suggestion(1)
    assert [p.label for p in picker.comps] == ["APASS B", "APASS A"]
    assert picker.target.coord == target_coord and len(picker.picks) == 3
    assert spec2.coord == STAR_A.coord
    # adding the same suggestion again is rejected (duplicate comp guard)
    with pytest.raises(SuggestionError, match="already a comp"):
        ctrl.add_suggestion(0)
    with pytest.raises(SuggestionError, match="already a comp"):
        ctrl.add_suggestion(-1)  # APASS A is already a comp now


def test_add_suggestion_negative_index(picker):
    ctrl = SuggestionController(picker, lookup=make_lookup([STAR_A, STAR_B]), radius_deg=1.5)
    ctrl.refresh()
    spec = ctrl.add_suggestion(-1)  # last suggestion = APASS A
    assert spec.label == "APASS A" and spec.coord == STAR_A.coord


def test_add_suggestion_errors(picker, image):
    ctrl = SuggestionController(picker, lookup=make_lookup([STAR_A]), radius_deg=1.5)
    ctrl.refresh()
    with pytest.raises(SuggestionError, match="out of range"):
        ctrl.add_suggestion(5)
    with pytest.raises(SuggestionError, match="out of range"):
        ctrl.add_suggestion(-2)
    with pytest.raises(SuggestionError, match="int"):
        ctrl.add_suggestion("0")  # ty: ignore[invalid-argument-type]
    with pytest.raises(SuggestionError, match="int"):
        ctrl.add_suggestion(True)
    # no pick controller attached
    ctrl.detach_picker()
    with pytest.raises(SuggestionError, match="no pick controller"):
        ctrl.add_suggestion(0)
    # no image: the sky->pixel conversion has no WCS to work with
    ctrl.attach_picker(picker)
    picker.set_image(None)
    with pytest.raises(SuggestionError, match="no image"):
        ctrl.add_suggestion(0)
    picker.set_image(image)
    # a star mapping outside the frame cannot be added — issue 08: it is
    # marked in_frame=False at refresh and refused with a clear SuggestionError
    far_but_in_radius = star("EDGE", 83.01, 21.98 + 1.4)  # ~1.4 deg -> far off-frame
    ctrl._lookup = make_lookup([far_but_in_radius])
    snap = ctrl.refresh()
    assert snap.suggestions[0].in_frame is False
    assert snap.out_of_frame_count == 1
    with pytest.raises(SuggestionError, match="outside the open frame"):
        ctrl.add_suggestion(0)
    assert picker.comps == ()


# --- following the target pick ----------------------------------------------------------------------


def test_controller_follows_target_changes(image):
    calls: list = []
    picker = PickController()
    picker.set_image(image)
    ctrl = SuggestionController(picker, lookup=make_lookup([STAR_A], calls), radius_deg=1.5)
    assert ctrl.snapshot.target is None  # no target yet
    picker.pick_at(40, 30, "target", snap=False)
    assert calls and calls[-1][0] == picker.target.coord  # auto re-search on target pick  # ty: ignore[missing-union-attribute]
    assert [s.name for s in ctrl.suggestions] == ["APASS A"]
    # moving the target re-searches with the new coord
    picker.pick_at(16, 16, "target", snap=False)
    assert calls[-1][0] == picker.target.coord  # ty: ignore[missing-union-attribute]
    assert calls[-1][0] != J2000Coord(ra_deg=83.01, dec_deg=21.98)
    # clearing the picks empties the suggestions
    picker.clear()
    assert ctrl.snapshot.target is None and ctrl.suggestions == ()
    # detach stops following
    ctrl.detach_picker()
    picker.pick_at(40, 30, "target", snap=False)
    assert ctrl.snapshot.target is None


def test_search_runner_is_used_for_automatic_searches(picker, target_coord):
    calls: list = []
    runner_calls: list = []
    ctrl = SuggestionController(picker, lookup=make_lookup([STAR_A], calls), radius_deg=1.5)
    assert calls  # the initial attach search ran inline
    inline_calls = len(calls)

    def runner():
        runner_calls.append(1)
        ctrl.refresh()  # what the widget's worker thread does

    ctrl.search_runner = runner
    assert ctrl.search_runner is runner
    # a target change triggers the runner, not an inline search
    picker.pick_at(16, 16, "target", snap=False)
    assert runner_calls == [1]
    assert len(calls) == inline_calls + 1 and calls[-1][0] == picker.target.coord
    # a radius change with a target set also goes through the runner
    ctrl.set_radius(2.0)
    assert runner_calls == [1, 1]
    assert calls[-1] == (picker.target.coord, 2.0)
    # clearing the runner restores inline searches
    ctrl.search_runner = None
    picker.pick_at(40, 30, "target", snap=False)
    assert len(calls) == inline_calls + 3 and runner_calls == [1, 1]


# --- listeners ---------------------------------------------------------------------------------------


def test_listeners_idempotent_decorator_isolated_and_frozen(picker):
    ctrl = SuggestionController(picker, lookup=make_lookup([STAR_A]))
    seen: list[SuggestionSnapshot] = []

    @ctrl.add_listener
    def observer(snap):
        seen.append(snap)

    ctrl.add_listener(observer)  # idempotent
    assert ctrl.listeners == (observer,)

    def broken(snap):
        raise RuntimeError("listener bug")

    ctrl.add_listener(broken)
    ctrl.refresh()  # the broken listener must not break the others or refresh
    assert len(seen) == 1 and seen[0].suggestions
    with pytest.raises(dataclasses.FrozenInstanceError):
        seen[0].radius_deg = 9.0  # type: ignore[misc]  # ty: ignore[invalid-assignment]
    ctrl.remove_listener(observer)
    ctrl.remove_listener(observer)  # no-op
    ctrl.refresh()
    assert len(seen) == 1  # unregistered -> not called again
    assert ctrl.listeners == (broken,)


# --- the default lookup is the lazy catalog facade ------------------------------------------------------


def test_default_lookup_delegates_to_the_catalog_facade(monkeypatch, target_coord):
    # importing the pure module never touches gi/ui/link/enhance (subprocess)
    code = (
        "import sys;"
        "import seestar_controller.photometry.suggest;"
        "assert 'gi' not in sys.modules;"
        "assert 'seestar_controller.ui' not in sys.modules;"
        "assert 'seestar_controller.link' not in sys.modules;"
        "assert 'seestar_controller.enhance' not in sys.modules;"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout

    # the default lookup instantiates the facade's Catalog at CALL time and
    # delegates to lookup_standards(coord, radius_deg)
    from seestar_controller.catalog import facade

    sentinel = [STAR_A]

    class FakeCatalog:
        def lookup_standards(self, coord, radius_deg, **kwargs):
            assert coord == target_coord and radius_deg == 1.5
            return sentinel

    monkeypatch.setattr(facade, "Catalog", FakeCatalog)
    ctrl = SuggestionController(lookup=suggest_module.default_standards_lookup, radius_deg=1.5)
    assert ctrl._lookup is suggest_module.default_standards_lookup
    picker = PickController()
    picker.set_image(FakeImage(synthetic_image(64), wcs_header()))
    ctrl.attach_picker(picker)
    picker.pick_at(40, 30, "target", snap=False)  # auto re-search via the facade
    snap = ctrl.refresh()
    assert [s.name for s in snap.suggestions] == ["APASS A"]


# --- save/load with a catalog-added comp ----------------------------------------------------------------


def test_save_load_round_trip_with_catalog_added_comp(wcs_stack_project):
    from seestar_controller.enhance import ViewerController

    project, frame = wcs_stack_project
    vc = ViewerController(project)
    vc.refresh_stacks()
    image = vc.open_stack(frame.id)
    picker = PickController(project=project, viewer_controller=vc)
    assert picker.image is image
    picker.pick_at(40, 30, "target", label="Target", snap=False)

    ctrl = SuggestionController(picker, lookup=make_lookup([STAR_A, STAR_B]), radius_deg=1.5)
    ctrl.refresh()
    comp = ctrl.add_suggestion(0)  # APASS B

    saved = picker.save()
    assert [p.role for p in saved] == ["target", "comp"]
    stored = load_star_picks(project)
    assert [p.role for p in stored] == ["target", "comp"]
    stored_comp = stored[1]
    assert stored_comp.label == "APASS B"
    assert stored_comp.ra_deg == pytest.approx(STAR_B.coord.ra_deg, abs=1e-9)
    assert stored_comp.dec_deg == pytest.approx(STAR_B.coord.dec_deg, abs=1e-9)
    # the stored pixel matches the WCS conversion of the star's sky position
    assert stored_comp.x == pytest.approx(comp.x, abs=1e-6)
    assert stored_comp.y == pytest.approx(comp.y, abs=1e-6)

    # a fresh controller on the same project restores the catalog-added comp
    reloaded = PickController(project=project)
    picks = reloaded.picks
    assert [p.role for p in picks] == ["target", "comp"]
    assert picks[1].label == "APASS B"
    assert picks[1].coord is not None
    assert picks[1].coord.ra_deg == pytest.approx(STAR_B.coord.ra_deg, abs=1e-9)
    assert picks[1].coord.dec_deg == pytest.approx(STAR_B.coord.dec_deg, abs=1e-9)
    # and the reloaded comp's pixel round-trips through the stored WCS
    back = pixel_to_sky(image.header, picks[1].x, picks[1].y)
    assert back.ra_deg == pytest.approx(STAR_B.coord.ra_deg, abs=1e-6)


# --- package boundary for the new pure module ------------------------------------------------------------


def test_suggest_module_source_has_no_gi_and_is_pure():
    import re

    source = (PHOTOMETRY_PACKAGE_DIR / "suggest.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE)
    assert not re.search(r"^\s*(from|import)\s+.*suggestions_panel", source, re.MULTILINE)
    code = (
        "import sys;"
        "import seestar_controller.photometry.suggest;"
        "assert 'gi' not in sys.modules;"
        "assert 'seestar_controller.ui' not in sys.modules;"
        "assert 'seestar_controller.link' not in sys.modules;"
        "assert 'seestar_controller.enhance' not in sys.modules;"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout
