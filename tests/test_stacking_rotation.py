"""Tests for the field-rotation handling of :mod:`seestar_controller.stacking`
(stacking issue 01 — ``.scratch/stacking/issues/01-blurry-stacks-from-field-rotation.md``):
on an alt-az mount the field rotates during a session, so the app-side
``stack_session_subs`` estimates each sub's rotation from its solved
``frame_wcs`` record (preferred) or its header ``CD`` matrix and DEROTATES
the subs to a common reference rotation before the median when the total
session rotation moves the farthest image corner by more than
:data:`~seestar_controller.stacking.DEROTATE_THRESHOLD_PX` (~1 star FWHM).
Without any WCS the combine stays a plain pixel-wise median (documented
fallback) and the result reports the rotation / a blur warning.

Pure tests: no ``gi``, no network, synthetic tmp projects only (the helper
fixtures/functions are shared with tests/test_stack_subs.py). The package
boundary is enforced in tests/test_stacking_boundary.py.
"""

import io
import math

import numpy as np
import pytest
from astropy.io import fits as pyfits
from astropy.wcs import WCS
from test_stack_subs import T0, date_obs_str

from seestar_controller.stacking import (
    DEROTATE_THRESHOLD_PX,
    StackSubsResult,
    derotate_array,
    header_rotation_deg,
    max_rotation_displacement_px,
    median_combine,
    rotation_exceeds_threshold,
    stack_session_subs,
    total_field_rotation_deg,
)
from seestar_controller.stacking.core import wrap_180
from seestar_controller.store import (
    KIND_SUB,
    close_session,
    create_project,
    list_sessions,
    read_frame_bytes,
    save_frame_wcs,
    start_session,
    store_frame,
)


@pytest.fixture()
def root(tmp_path):
    """A tmp project root."""
    r = tmp_path / "projects"
    r.mkdir()
    return r


@pytest.fixture()
def project(root):
    """A synthetic project (closed on teardown)."""
    p = create_project("M 42 night", root=root)
    yield p
    p.close()


@pytest.fixture()
def session(project):
    """One open session in the project (closed on teardown)."""
    sid = start_session(project, started_at=T0)
    yield sid
    for s in list_sessions(project):
        if s.id == sid and s.ended_at is None:
            close_session(project, sid)

SIZE = 64
CENTER_RA = 100.0
CENTER_DEC = 20.0
SCALE_ARCSEC = 2.0


# --- helpers ---------------------------------------------------------------------------


def wcs_cards(rotation_deg: float, shape=(SIZE, SIZE)) -> dict:
    """A TAN WCS as FITS cards with the field rotated by ``rotation_deg``
    (the position angle of north, east of north — ``atan2(CD1_2, CD2_2)``)."""
    scale_deg = SCALE_ARCSEC / 3600.0
    th = math.radians(rotation_deg)
    ny, nx = shape
    return {
        "WCSAXES": 2,
        "CTYPE1": "RA---TAN",
        "CTYPE2": "DEC--TAN",
        "CUNIT1": "deg",
        "CUNIT2": "deg",
        "CRVAL1": CENTER_RA,
        "CRVAL2": CENTER_DEC,
        "CRPIX1": (nx + 1) / 2.0,
        "CRPIX2": (ny + 1) / 2.0,
        "CD1_1": -scale_deg * math.cos(th),
        "CD1_2": scale_deg * math.sin(th),
        "CD2_1": scale_deg * math.sin(th),
        "CD2_2": scale_deg * math.cos(th),
    }


def fits_bytes_wcs(data: np.ndarray, cards: dict, *, date_obs: str) -> bytes:
    """A small valid FITS file with the given data, DATE-OBS and extra cards."""
    hdu = pyfits.PrimaryHDU(data=np.asarray(data, dtype=np.float32))
    hdu.header["DATE-OBS"] = date_obs
    hdu.header["EXPTIME"] = 10.0
    for key, value in cards.items():
        hdu.header[key] = value
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def _wcs(cards: dict) -> WCS:
    return WCS(pyfits.Header(dict(cards)), naxis=2)


def star_pixel(cards: dict, star_ra: float, star_dec: float) -> tuple[int, int]:
    """The (row, col) pixel of a sky position under the given WCS cards."""
    x, y = _wcs(cards).all_world2pix(star_ra, star_dec, 0)
    return round(float(y)), round(float(x))


def star_field(rotations, *, radius_px=25.0, peak=1000.0, background=100.0, sigma=1.5):
    """Build one sub per rotation: a flat background plus one Gaussian star
    (FWHM ~3.5 px, like a real seeing disk) at the SAME sky position (due
    east of the field center in the reference frame), which lands on a
    different pixel in each rotated sub.

    Returns ``(datas, cards_list, star_ra, star_dec)``.
    """
    ref_wcs = _wcs(wcs_cards(0.0))
    cx = cy = (SIZE - 1) / 2.0
    east = ref_wcs.pixel_to_world(cx + radius_px, cy)  # due east in the ref frame
    star_ra, star_dec = east.ra.deg, east.dec.deg
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    datas, cards_list = [], []
    for rotation in rotations:
        cards = wcs_cards(rotation)
        x, y = _wcs(cards).all_world2pix(star_ra, star_dec, 0)
        data = np.full((SIZE, SIZE), background, dtype=np.float64)
        data += peak * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sigma**2))
        datas.append(data.astype(np.float32))
        cards_list.append(cards)
    return datas, cards_list, star_ra, star_dec


def _store_sub(project, sid, data, cards, i, seconds=10):
    return store_frame(
        project,
        sid,
        KIND_SUB,
        fits_bytes_wcs(data, cards, date_obs=date_obs_str(i, seconds=seconds)),
    )


def _read_stack(project, frame):
    raw = read_frame_bytes(project, frame)
    with pyfits.open(io.BytesIO(raw), memmap=False) as hdul:
        return hdul[0].data, hdul[0].header


def _bright_pixel_count(data: np.ndarray, fraction: float = 0.5) -> int:
    """How many pixels are above ``fraction`` of the peak ABOVE the
    background (a sharp star: ~a dozen; a smeared arc: many more)."""
    clean = np.nan_to_num(np.asarray(data, dtype=np.float64), nan=0.0)
    peak = float(clean.max())
    background = float(np.median(clean))
    return int(np.count_nonzero(clean >= background + fraction * (peak - background)))


def _peak(data: np.ndarray) -> float:
    return float(np.nanmax(np.asarray(data, dtype=np.float64)))


# --- header_rotation_deg: pure card math ------------------------------------------------


def test_header_rotation_deg_north_up_cdelt_is_zero():
    cards = {
        "CTYPE1": "RA---TAN",
        "CTYPE2": "DEC--TAN",
        "CDELT1": -SCALE_ARCSEC / 3600.0,
        "CDELT2": SCALE_ARCSEC / 3600.0,
        "CRVAL1": CENTER_RA,
        "CRVAL2": CENTER_DEC,
    }
    assert header_rotation_deg(cards) == 0.0


def test_header_rotation_deg_from_cd_matrix():
    for rotation in (0.0, 10.0, 30.0, -45.0, 179.0):
        assert header_rotation_deg(wcs_cards(rotation)) == pytest.approx(rotation, abs=1e-9)


def test_header_rotation_deg_from_pc_matrix_times_cdelt():
    th = math.radians(25.0)
    scale = SCALE_ARCSEC / 3600.0
    cards = {
        "CTYPE1": "RA---TAN",
        "CTYPE2": "DEC--TAN",
        "CRVAL1": CENTER_RA,
        "CRVAL2": CENTER_DEC,
        "CDELT1": -scale,
        "CDELT2": scale,
        "PC1_1": math.cos(th),
        "PC1_2": math.sin(th),
        "PC2_1": -math.sin(th),
        "PC2_2": math.cos(th),
    }
    # CD = PC @ diag(CDELT) -> atan2(CD1_2, CD2_2) = 25 deg
    assert header_rotation_deg(cards) == pytest.approx(25.0, abs=1e-9)


def test_header_rotation_deg_none_without_wcs_and_type_error():
    assert header_rotation_deg({"OBJECT": "M 42"}) is None
    assert header_rotation_deg({"CD1_1": 0.0, "CD1_2": 0.0, "CD2_1": 0.0, "CD2_2": 0.0}) is None
    with pytest.raises(TypeError):
        header_rotation_deg(["not", "a", "mapping"])  # ty: ignore[invalid-argument-type]


# --- total_field_rotation_deg / displacement / threshold ---------------------------------


def test_wrap_180():
    assert wrap_180(350.0) == pytest.approx(-10.0)
    assert wrap_180(-190.0) == pytest.approx(170.0)
    assert wrap_180(10.0) == pytest.approx(10.0)


def test_total_field_rotation_deg_largest_pairwise_difference():
    assert total_field_rotation_deg([0.0, 10.0, 25.0]) == pytest.approx(25.0)
    assert total_field_rotation_deg([350.0, 10.0]) == pytest.approx(20.0)  # wrap
    assert total_field_rotation_deg([170.0, -170.0]) == pytest.approx(20.0)  # fold >180
    assert total_field_rotation_deg([5.0, 5.0, 5.0]) == 0.0
    assert total_field_rotation_deg([7.0]) is None  # fewer than 2 -> None
    assert total_field_rotation_deg([]) is None
    assert total_field_rotation_deg([0.0, None]) is None  # type: ignore[list-item]
    assert total_field_rotation_deg([0.0, float("nan")]) is None
    with pytest.raises(TypeError):
        total_field_rotation_deg(5.0)  # type: ignore[invalid-argument-type]  # ty: ignore[invalid-argument-type]


def test_max_rotation_displacement_px_corner_arc():
    # (100, 100) image: corner radius = hypot(100, 100)/2 = 70.71 px
    expected = math.hypot(100, 100) / 2.0 * math.radians(90.0)
    assert max_rotation_displacement_px(90.0, (100, 100)) == pytest.approx(expected)
    assert max_rotation_displacement_px(0.0, (100, 100)) == 0.0
    with pytest.raises(TypeError):
        max_rotation_displacement_px("30", (10, 10))  # type: ignore[invalid-argument-type]  # ty: ignore[invalid-argument-type]
    from seestar_controller.stacking import StackSubsError

    with pytest.raises(StackSubsError):
        max_rotation_displacement_px(30.0, (10,))  # type: ignore[invalid-argument-type]


def test_rotation_exceeds_threshold_default_is_derotate_threshold_px():
    # 64x64 image: corner radius ~45.25 px; 30 deg -> ~23.7 px (way above)
    assert rotation_exceeds_threshold(30.0, (SIZE, SIZE))
    # 1 deg -> ~0.79 px (below the ~1 FWHM / 2 px threshold)
    assert not rotation_exceeds_threshold(1.0, (SIZE, SIZE))
    assert DEROTATE_THRESHOLD_PX >= 1.0  # documented ~1 FWHM at ~2 px seeing


# --- derotate_array ----------------------------------------------------------------------


def test_derotate_array_aligns_a_rotated_star_with_astropy():
    """The sign convention, verified against astropy WCS: a sub with field
    rotation 30 deg derotated by ``ref - sub = -30`` deg puts a star at the
    same pixel as the reference (unrotated) frame."""
    cards30 = wcs_cards(30.0)
    ref_wcs = _wcs(wcs_cards(0.0))
    cx = cy = (SIZE - 1) / 2.0
    star = ref_wcs.pixel_to_world(cx + 20, cy)  # due east in the reference frame
    sub = np.zeros((SIZE, SIZE), dtype=np.float32)
    row, col = star_pixel(cards30, star.ra.deg, star.dec.deg)
    sub[row, col] = 1.0

    derotated = derotate_array(sub, 0.0 - 30.0)  # ref_rotation - sub_rotation

    assert derotated.shape == sub.shape
    # the star is now at the reference pixel (within interpolation error)
    out_row, out_col = star_pixel(wcs_cards(0.0), star.ra.deg, star.dec.deg)
    peak_row, peak_col = np.unravel_index(np.nanargmax(derotated), derotated.shape)
    assert abs(int(peak_row) - out_row) <= 1
    assert abs(int(peak_col) - out_col) <= 1
    # the rotated-in corners are NaN
    assert np.isnan(derotated[0, 0]) or np.isnan(derotated[-1, -1])


def test_derotate_array_zero_angle_returns_a_copy_and_3d_works():
    arr = np.arange(16, dtype=np.float32).reshape(4, 4)
    out = derotate_array(arr, 0.0)
    assert out is not arr and np.array_equal(out, arr)
    rgb = np.zeros((8, 8, 3), dtype=np.float32)
    rgb[4, 6, :] = 1.0
    out3 = derotate_array(rgb, 15.0)
    assert out3.shape == (8, 8, 3)
    assert np.isnan(out3[0, 0, 0])


def test_derotate_array_int_input_and_errors():
    ints = np.zeros((8, 8), dtype=np.int16)
    ints[4, 4] = 5
    out = derotate_array(ints, 10.0)
    assert np.issubdtype(out.dtype, np.floating)
    with pytest.raises(TypeError):
        derotate_array([1, 2, 3], 10.0)  # type: ignore[invalid-argument-type]
    from seestar_controller.stacking import StackSubsError

    with pytest.raises(StackSubsError):
        derotate_array(np.zeros(5), 10.0)
    with pytest.raises(TypeError):
        derotate_array(np.zeros((4, 4)), "10")  # type: ignore[invalid-argument-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(StackSubsError):
        derotate_array(np.zeros((4, 4)), float("nan"))


# --- median_combine nan_safe -------------------------------------------------------------


def test_median_combine_nan_safe_ignores_nan_corners():
    a = np.full((6, 6), 1.0, dtype=np.float32)
    b = np.full((6, 6), 3.0, dtype=np.float32)
    a[0, 0] = np.nan
    b[5, 5] = np.nan
    combined = median_combine([a, b], nan_safe=True)
    assert combined.dtype == np.float32
    assert combined[0, 0] == pytest.approx(3.0)  # NaN in only one sub -> the other
    assert combined[5, 5] == pytest.approx(1.0)
    assert combined[3, 3] == pytest.approx(2.0)
    both_nan = median_combine([a, b.copy()], nan_safe=True)
    a2 = a.copy()
    b2 = b.copy()
    a2[2, 2] = np.nan
    b2[2, 2] = np.nan
    all_nan = median_combine([a2, b2], nan_safe=True)
    assert np.isnan(all_nan[2, 2])  # NaN in every sub stays NaN (no warning crash)
    assert both_nan[0, 0] == pytest.approx(3.0)


def test_median_combine_default_still_plain_median():
    a = np.full((4, 4), 1.0, dtype=np.float32)
    b = np.full((4, 4), 3.0, dtype=np.float32)
    assert np.allclose(median_combine([a, b]), 2.0)


# --- stack_session_subs: rotation estimation + derotation --------------------------------


def test_stack_derotates_rotated_subs_with_header_cd_and_stays_sharp(project, session):
    """The issue-01 scenario via header CD matrices: 3 subs whose fields are
    rotated 0/15/30 deg share one star at the same SKY position; the stack
    derotates them, so the star stays a single sharp pixel instead of a
    13 px arc."""
    rotations = (0.0, 15.0, 30.0)
    datas, cards_list, star_ra, star_dec = star_field(rotations)
    for i, (data, cards) in enumerate(zip(datas, cards_list)):
        _store_sub(project, session, data, cards, i)

    result = stack_session_subs(project, session)

    assert result.n_subs == 3
    assert result.derotated is True
    assert result.rotation_source == "header"
    assert result.field_rotation_deg == pytest.approx(30.0, abs=1e-6)
    assert result.rotation_displacement_px == pytest.approx(
        max_rotation_displacement_px(30.0, (SIZE, SIZE))
    )
    assert result.max_derotation_deg == pytest.approx(15.0, abs=1e-6)
    assert result.rotation_warning is None
    data, header = _read_stack(project, result.frame)
    # sharp: the star keeps its full peak (a plain median of the rotated subs
    # smears it to ~170) and is one compact blob, not an arc
    assert _peak(data) > 900.0
    assert _bright_pixel_count(data) <= 20
    # the peak sits at the reference-rotation pixel of the star
    ref_row, ref_col = star_pixel(wcs_cards(15.0), star_ra, star_dec)  # median rotation
    peak_row, peak_col = np.unravel_index(np.argmax(data), data.shape)
    assert abs(int(peak_row) - ref_row) <= 1
    assert abs(int(peak_col) - ref_col) <= 1
    # HISTORY records the rotation and the derotation
    history = str(header["HISTORY"])
    assert "field rotation over the session: 30.00 deg" in history
    assert "derotated 3 subs" in history


def test_stack_without_any_wcs_falls_back_to_plain_median_and_smears(project, session):
    """Control: the same rotated subs but with the WCS cards stripped — no
    rotation estimate at all -> plain pixel-wise median (documented
    fallback), the star smears into an arc, and the result reports
    field_rotation_deg None."""
    rotations = (0.0, 15.0, 30.0)
    datas, _cards, _ra, _dec = star_field(rotations)
    plain_cards = {"OBJECT": "M 42"}  # no CD matrix, no CTYPE
    for i, data in enumerate(datas):
        _store_sub(project, session, data, plain_cards, i)

    result = stack_session_subs(project, session)

    assert result.derotated is False
    assert result.rotation_source is None
    assert result.field_rotation_deg is None
    assert result.rotation_displacement_px is None
    assert result.rotation_warning is None
    data, _header = _read_stack(project, result.frame)
    # smeared away: 30 deg at r=25 px spreads the star into a ~13 px arc, so
    # the pixel-wise median keeps only ~17% of its peak (vs >900 derotated)
    assert _peak(data) < 400.0


def test_stack_prefers_frame_wcs_over_header_and_derotates(project, session):
    """Solved frame_wcs records win over the header CD matrix as the rotation
    source (astrometry-solved subs get derotated)."""
    rotations = (0.0, 20.0, 40.0)
    datas, cards_list, _ra, _dec = star_field(rotations)
    plain_cards = {"OBJECT": "M 42"}
    frames = []
    for i, data in enumerate(datas):
        frame = _store_sub(project, session, data, plain_cards, i)
        save_frame_wcs(
            project,
            frame.id,
            solver="solve-field",
            n_stars=42,
            center_ra_deg=CENTER_RA,
            center_dec_deg=CENTER_DEC,
            cards=cards_list[i],
            rms_arcsec=0.4,
        )
        frames.append(frame)

    result = stack_session_subs(project, session)

    assert result.derotated is True
    assert result.rotation_source == "frame_wcs"
    assert result.field_rotation_deg == pytest.approx(40.0, abs=1e-6)
    data, header = _read_stack(project, result.frame)
    assert _peak(data) > 900.0
    assert _bright_pixel_count(data) <= 20
    assert "source: frame_wcs" in str(header["HISTORY"])


def test_stack_warns_but_still_medians_when_not_all_subs_have_a_wcs(project, session):
    """Only 2 of 3 subs solved: no derotation (a rotation estimate is needed
    for EVERY combined sub), but the total rotation is still computed and
    the result carries the blur warning for the UI."""
    rotations = (0.0, 20.0, 40.0)
    datas, cards_list, _ra, _dec = star_field(rotations)
    plain_cards = {"OBJECT": "M 42"}  # no header CD: only the frame_wcs solves count
    for i, data in enumerate(datas):
        frame = _store_sub(project, session, data, plain_cards, i)
        if i < 2:
            save_frame_wcs(
                project,
                frame.id,
                solver="gaia",
                n_stars=30,
                center_ra_deg=CENTER_RA,
                center_dec_deg=CENTER_DEC,
                cards=cards_list[i],
            )

    result = stack_session_subs(project, session)

    assert result.derotated is False
    assert result.rotation_source == "frame_wcs"
    assert result.field_rotation_deg == pytest.approx(20.0, abs=1e-6)
    assert result.rotation_displacement_px is not None
    assert result.rotation_displacement_px >= DEROTATE_THRESHOLD_PX
    warning = result.rotation_warning
    assert warning is not None
    assert "20.0°" in warning and "blurry" in warning
    data, _header = _read_stack(project, result.frame)
    assert _peak(data) < 400.0  # not derotated -> the star is smeared


def test_stack_below_threshold_keeps_plain_median_but_reports_rotation(project, session):
    """A tiny rotation (0.1 deg -> ~0.08 px at the corner) is below the
    threshold: no derotation, but the total rotation is still reported."""
    rotations = (0.0, 0.05, 0.1)
    datas, cards_list, _ra, _dec = star_field(rotations)
    for i, (data, cards) in enumerate(zip(datas, cards_list)):
        _store_sub(project, session, data, cards, i)

    result = stack_session_subs(project, session)

    assert result.derotated is False
    assert result.field_rotation_deg == pytest.approx(0.1, abs=1e-9)
    assert result.rotation_displacement_px == pytest.approx(
        max_rotation_displacement_px(0.1, (SIZE, SIZE))
    )
    assert result.rotation_displacement_px is not None
    assert result.rotation_displacement_px < DEROTATE_THRESHOLD_PX
    assert result.rotation_warning is None
    data, header = _read_stack(project, result.frame)
    assert "field rotation over the session: 0.10 deg" in str(header["HISTORY"])
    assert "derotated" not in str(header["HISTORY"])


def test_stack_result_defaults_keep_the_old_three_field_shape():
    """StackSubsResult still constructs with the original three fields; the
    rotation fields default to the no-WCS fallback."""
    sentinel = object()
    result = StackSubsResult(frame=sentinel, n_subs=3, skipped=())  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    assert result.frame is sentinel
    assert result.n_subs == 3
    assert result.field_rotation_deg is None
    assert result.derotated is False
    assert result.rotation_source is None
    assert result.max_derotation_deg == 0.0
    assert result.rotation_warning is None


def test_stack_single_sub_has_no_rotation_to_report(project, session):
    data, cards_list, _ra, _dec = star_field((12.0,))
    _store_sub(project, session, data, cards_list[0], 0)

    result = stack_session_subs(project, session)

    assert result.n_subs == 1
    assert result.field_rotation_deg is None  # fewer than 2 rotations
    assert result.derotated is False
    assert result.rotation_source == "header"  # the estimate exists, just no span
    data_out, _header = _read_stack(project, result.frame)
    assert np.allclose(data_out, data, atol=1e-6)  # untouched single sub
