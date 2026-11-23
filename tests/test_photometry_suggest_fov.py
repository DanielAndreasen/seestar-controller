"""Issue 08: catalog comp suggestions are limited to the open frame's FOV
(:mod:`seestar_controller.photometry.suggest` + :mod:`.picking`).

Headless: no ``gi`` anywhere. The catalog is MOCKED (the injectable ``lookup``
callable), the WCS is a synthetic CAR header built via :class:`astropy.wcs.WCS`
— the same pattern as tests/test_photometry_suggest.py.

Covered:

- the default comp-search radius reflects the S50 FOV (~0.5 deg, not the old
  1.5 deg) in both the pure layer (:data:`.suggest.DEFAULT_COMP_SEARCH_RADIUS_DEG`)
  and the settings (:data:`seestar_controller.ui.state.DEFAULT_COMP_SEARCH_RADIUS_DEG`);
- the frame-footprint helpers :func:`.picking.frame_contains_sky` (is a sky
  position inside the frame, via ``sky_to_pixel`` + pixel bounds) and
  :func:`.picking.frame_fov_radius_deg` (the WCS-derived FOV half-diagonal of
  the open frame; None without a WCS);
- :meth:`SuggestionController.refresh` marks every suggestion with
  :attr:`CompSuggestion.in_frame` against the pick controller's current image,
  counts the out-of-frame ones in :attr:`SuggestionSnapshot.out_of_frame_count`
  and exposes the frame FOV as :attr:`SuggestionSnapshot.frame_fov_deg`;
  out-of-frame suggestions stay listed (the widget greys them out) but
  :meth:`SuggestionController.add_suggestion` refuses them;
- without a usable WCS on the current frame the footprint is undeterminable:
  every suggestion is marked in-frame and no FOV is reported;
- backwards compatibility: ``CompSuggestion`` / ``SuggestionSnapshot``
  positional construction without the new fields keeps working.

The widget layer (greyed/disabled out-of-frame rows in
:class:`~seestar_controller.photometry.suggestions_panel.SuggestionsPanel`) is
tested in tests/test_photometry_widgets.py (skips headless).
"""

import numpy as np
import pytest

from seestar_controller.catalog.coords import J2000Coord, angular_distance
from seestar_controller.catalog.standards import StandardStar
from seestar_controller.photometry import (
    DEFAULT_COMP_SEARCH_RADIUS_DEG,
    NO_WCS_HINT,
    CompSuggestion,
    PickController,
    SuggestionController,
    SuggestionError,
    SuggestionSnapshot,
    frame_contains_sky,
    frame_fov_radius_deg,
    has_celestial_wcs,
    pixel_to_sky,
    sky_to_pixel,
)
from seestar_controller.photometry import suggest as suggest_module
from seestar_controller.ui import state as ui_state

# --- synthetic WCS header + image (same pattern as test_photometry_suggest) -------

#: The synthetic frame: 64x64, CAR WCS, CRVAL (83, 22) at 1-based pixel (51, 51)
#: -> 0-based center (50, 50); 0.001 deg/px, so the frame spans
#: ra 82.987..83.05 / dec 21.987..22.05.
CRVAL = (83.0, 22.0)


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


def no_wcs_header() -> dict:
    """A Seestar-style SUB header: NAXIS=2, NO CTYPE cards (no celestial WCS)."""
    return {
        "SIMPLE": True,
        "BITPIX": 16,
        "NAXIS": 2,
        "NAXIS1": 64,
        "NAXIS2": 64,
        "RA": 359.47083,
        "DEC": 56.726111,
        "DATE-OBS": "2026-11-20T21:33:33.024310",
        "OBJECT": "M 31",
    }


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
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    data = np.full((size, size), 10.0, dtype=np.float64)
    for cy, cx, peak in ((30, 40, 8000.0), (16, 16, 5000.0)):
        data = data + peak * np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / 8.0))
    data = data + rng.normal(0.0, 1.0, size=(size, size))
    return np.ascontiguousarray(data, dtype=np.float32)


def star(name: str, ra_deg: float, dec_deg: float, mag: float | None = 12.5, band: str = "V"):
    return StandardStar(
        name=name,
        coord=J2000Coord(ra_deg=ra_deg, dec_deg=dec_deg),
        magnitude=mag,
        band=band,
        distance_deg=0.0,
    )


def make_lookup(stars, calls: list | None = None):
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
    """A PickController with the synthetic WCS image and a target at pixel
    (40, 30) — sky (83.01, 21.98) on the CAR WCS."""
    ctrl = PickController()
    ctrl.set_image(image)
    ctrl.pick_at(40, 30, "target", label="Target", snap=False)
    return ctrl


#: Stars around the target (83.01, 21.98): A, B, E map INSIDE the 64x64 frame
#: (pixels ~(40, 50), (45, 30), (42, 55)); C (~2 deg) and D (~1.5 deg) map far
#: outside it.
STAR_A = star("APASS A", 83.01, 22.00, mag=11.2)  # 0.02 deg, on-frame
STAR_B = star("APASS B", 83.005, 21.98, mag=13.4, band="R")  # ~0.0046 deg, on-frame
STAR_E = star("APASS E", 83.008, 22.005, mag=12.3)  # ~0.025 deg, on-frame
STAR_C = star("APASS C", 83.01, 24.0, mag=9.9)  # ~2 deg, off-frame
STAR_D = star("APASS D", 83.01, 23.48, mag=None, band="B")  # ~1.5 deg, off-frame


# --- the default radius reflects the FOV (issue 08) --------------------------------


def test_default_radius_reflects_the_s50_fov_not_1_5_deg():
    assert DEFAULT_COMP_SEARCH_RADIUS_DEG == suggest_module.DEFAULT_COMP_SEARCH_RADIUS_DEG == 0.5
    assert ui_state.DEFAULT_COMP_SEARCH_RADIUS_DEG == 0.5
    assert ui_state.DEFAULT_SETTINGS["photometry_comp_search_radius_deg"] == "0.5"
    assert SuggestionController().radius_deg == 0.5


# --- the frame-footprint helpers -----------------------------------------------------


def test_frame_contains_sky_uses_sky_to_pixel_and_pixel_bounds(image):
    # on-frame stars map inside [0, 64) x [0, 64)
    for s in (STAR_A, STAR_B, STAR_E):
        x, y = sky_to_pixel(image.header, s.coord)
        assert 0.0 <= x < 64 and 0.0 <= y < 64
        assert frame_contains_sky(image, s.coord) is True
    # off-frame stars (C, D) map outside -> False
    assert frame_contains_sky(image, STAR_C.coord) is False
    assert frame_contains_sky(image, STAR_D.coord) is False
    # a star just outside an edge is out too
    edge = star("EDGE", 83.01, 22.06)  # dec above the frame (max 22.05)
    assert frame_contains_sky(image, edge.coord) is False
    # undeterminable: no image / no WCS -> None (never raises)
    assert frame_contains_sky(None, STAR_A.coord) is None
    no_wcs = FakeImage(synthetic_image(64), no_wcs_header())
    assert not has_celestial_wcs(no_wcs.header)
    assert frame_contains_sky(no_wcs, STAR_A.coord) is None


def test_frame_fov_radius_deg_is_the_wcs_half_diagonal(image):
    fov = frame_fov_radius_deg(image)
    assert fov is not None
    # 64 px at 0.001 deg/px: geometric center (31.5, 31.5) to a corner (0, 0)
    # is 31.5 px in each axis ~ 0.0445 deg on the sky (ra scaled by cos(dec))
    center = pixel_to_sky(image.header, 31.5, 31.5)
    corner = pixel_to_sky(image.header, 0.0, 0.0)
    expected = angular_distance(center, corner)
    assert fov == pytest.approx(expected, abs=1e-6)  # max over the 4 corners
    assert fov == pytest.approx(0.0445, abs=0.005)  # sanity: ~0.045 deg, NOT 1.5 deg
    # undeterminable without an image / WCS -> None
    assert frame_fov_radius_deg(None) is None
    assert frame_fov_radius_deg(FakeImage(synthetic_image(64), no_wcs_header())) is None


# --- refresh marks suggestions against the open frame's footprint --------------------


def test_refresh_marks_in_frame_and_counts_out_of_frame(picker):
    ctrl = SuggestionController(
        picker, lookup=make_lookup([STAR_A, STAR_B, STAR_E, STAR_C, STAR_D]), radius_deg=2.5
    )
    snap = ctrl.refresh()
    # all five are within the 2.5 deg radius and stay listed, nearest-first
    assert [s.name for s in snap.suggestions] == [
        "APASS B",
        "APASS A",
        "APASS E",
        "APASS D",
        "APASS C",
    ]
    by_name = {s.name: s for s in snap.suggestions}
    assert by_name["APASS A"].in_frame is True
    assert by_name["APASS B"].in_frame is True
    assert by_name["APASS E"].in_frame is True
    assert by_name["APASS C"].in_frame is False
    assert by_name["APASS D"].in_frame is False
    # the snapshot reports the out-of-frame count and the frame FOV (issue 08)
    assert snap.out_of_frame_count == 2
    assert ctrl.out_of_frame_count == 2
    assert snap.frame_fov_deg == pytest.approx(frame_fov_radius_deg(picker.image))
    assert ctrl.frame_fov_radius_deg == pytest.approx(frame_fov_radius_deg(picker.image))
    # the describe() label says it too
    assert "outside the frame" in by_name["APASS C"].describe()
    assert "outside the frame" not in by_name["APASS A"].describe()


def test_refresh_out_of_frame_count_resets_on_error_and_empty_states(picker):
    def failing(coord, radius_deg):
        raise RuntimeError("offline")

    ctrl = SuggestionController(picker, lookup=make_lookup([STAR_C]), radius_deg=2.5)
    ctrl.refresh()
    assert ctrl.snapshot.out_of_frame_count == 1
    # a catalog failure -> clean error, empty suggestions, count reset
    ctrl._lookup = failing
    snap = ctrl.refresh()
    assert snap.error is not None and "offline" in snap.error
    assert snap.suggestions == () and snap.out_of_frame_count == 0
    # recovery
    ctrl._lookup = make_lookup([STAR_A])
    snap = ctrl.refresh()
    assert snap.error is None and snap.out_of_frame_count == 0
    assert snap.suggestions[0].in_frame is True
    # no target -> empty + count reset
    picker.clear()
    snap = ctrl.refresh()
    assert snap.target is None and snap.suggestions == () and snap.out_of_frame_count == 0


def test_add_suggestion_refuses_out_of_frame_but_adds_in_frame(picker, image):
    ctrl = SuggestionController(
        picker, lookup=make_lookup([STAR_A, STAR_B, STAR_C]), radius_deg=2.5
    )
    snap = ctrl.refresh()
    assert [s.name for s in snap.suggestions] == ["APASS B", "APASS A", "APASS C"]
    # the out-of-frame suggestion (APASS C, index 2) is NOT addable
    with pytest.raises(SuggestionError, match="outside the open frame"):
        ctrl.add_suggestion(2)
    assert picker.comps == ()
    # the in-frame ones are added at their exact WCS pixel, as before
    spec = ctrl.add_suggestion(0)  # APASS B
    assert spec.label == "APASS B" and spec.role == "comp"
    expected_x, expected_y = sky_to_pixel(image.header, STAR_B.coord)
    assert (spec.x, spec.y) == pytest.approx((expected_x, expected_y), abs=1e-9)
    assert len(picker.comps) == 1


def test_frame_without_wcs_marks_everything_in_frame_and_reports_no_fov(picker):
    """The footprint is only determinable with a celestial WCS on the CURRENT
    image: target picked on a solved frame, then a raw sub opened — the
    suggestions stay listed, all marked in-frame, and no FOV is reported."""
    ctrl = SuggestionController(picker, lookup=make_lookup([STAR_A, STAR_C]), radius_deg=2.5)
    assert ctrl.refresh().out_of_frame_count == 1  # on the WCS frame, C is out
    picker.set_image(FakeImage(synthetic_image(64), no_wcs_header()))  # picks are kept
    assert picker.target is not None and picker.target.coord is not None
    snap = ctrl.refresh()
    assert snap.error is None  # the target still has sky coords -> normal search
    assert [s.name for s in snap.suggestions] == ["APASS A", "APASS C"]
    assert all(s.in_frame for s in snap.suggestions)  # footprint undeterminable
    assert snap.out_of_frame_count == 0
    assert snap.frame_fov_deg is None and ctrl.frame_fov_radius_deg is None


def test_pixel_only_target_still_gives_the_plate_solve_hint():
    """Ticket 05 unchanged: a frame without a WCS -> no sky coord -> the
    plate-solve hint, no lookup, no FOV."""
    calls: list = []
    picker = PickController()
    picker.set_image(FakeImage(synthetic_image(64), no_wcs_header()))
    picker.pick_at(40, 30, "target", snap=False)
    ctrl = SuggestionController(picker, lookup=make_lookup([STAR_A], calls), radius_deg=2.5)
    snap = ctrl.snapshot
    assert snap.error == NO_WCS_HINT and snap.suggestions == () and calls == []
    assert snap.frame_fov_deg is None and snap.out_of_frame_count == 0


# --- backwards compatibility of the frozen dataclasses --------------------------------


def test_comp_suggestion_and_snapshot_positional_construction_still_works():
    coord = J2000Coord(ra_deg=83.01, dec_deg=22.0)
    s = CompSuggestion(name="X", coord=coord, magnitude=12.0, band="V", distance_deg=0.1)
    assert s.in_frame is True  # default
    snap = SuggestionSnapshot(target=coord, radius_deg=0.5, suggestions=(s,), error=None)
    assert snap.sort_mode == "distance" and snap.frame_fov_deg is None
    assert snap.out_of_frame_count == 0
    # the new fields are accepted positionally too
    snap2 = SuggestionSnapshot(coord, 0.5, (s,), None, "magnitude", 0.066, 1)
    assert snap2.sort_mode == "magnitude" and snap2.frame_fov_deg == 0.066
    assert snap2.out_of_frame_count == 1


def test_frame_footprint_helpers_are_exported_from_the_package():
    from seestar_controller import photometry

    assert "frame_contains_sky" in photometry.__all__
    assert "frame_fov_radius_deg" in photometry.__all__
    assert photometry.frame_contains_sky is frame_contains_sky
    assert photometry.frame_fov_radius_deg is frame_fov_radius_deg
