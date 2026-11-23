"""Pure-layer tests for photometry issue 09 — per-frame WCS reprojection of
sky picks in measure/run (fixed pixel picks used to silently skip drifted or
rotated frames).

Headless: no ``gi`` anywhere (the sandbox has no GTK, see
docs/wiki/assumptions.md). The synthetic session mimics a live alt-az night:
EVERY sub-frame carries its own solved WCS (a plain CAR WCS whose reference
pixel drifts by (20, 15) px per frame — field rotation + tracking drift) and
the stars are drawn at the position the pick's sky coordinate maps to in THAT
frame, so a fixed pixel pick only works for frame 0: on the drifted frames the
star is >= 25 px away from the fixed pick, so the fixed pixel collects only
photometric dust (~1e-6 ADU instead of ~20000) and the frame fails. The picks
are saved with BOTH pixel and sky positions (``StarPick.x``/``y`` +
``ra_deg``/``dec_deg``), exactly like the pick session of the reported live
run.

Covered: the lenient :func:`~seestar_controller.photometry.picking.try_sky_to_pixel`
helper, :func:`~seestar_controller.photometry.measure.resolve_pick_pixels`
(reprojection + per-pick fallback), ``measure_frame_data``'s ``wcs=`` argument
(FrameWcs record / cards mapping / astropy WCS, reprojected-outside-image
skip), :func:`~seestar_controller.photometry.measure.measure_frame` /
:func:`~seestar_controller.photometry.measure.measure_project` on stored frames
with per-frame WCS (all drifted frames recovered; pixel-only picks on the same
frames skip), :meth:`~seestar_controller.photometry.run.RunController.run_session`
(WCS reprojection per frame, unsolved frame falls back to fixed pixels,
aggregated skip reasons + WCS-vs-fallback counts on the :class:`RunReport`,
persisted into ``project.json``), the pure report aggregation properties, and
the package boundary (subprocess + source scan).

Issue 12 (this file's second half): centroid refinement of the WCS-reprojected
picks on the frame data — the solve WCS is ~5 px off, so an unrefined pick
misses the star core with the r=4 aperture while the 8-12 px annulus catches
the wings (non-positive flux -> skip). Covered: :func:`refine_pick_on_frame`
(offset star centroided, no-star/outside-window/outside-image -> None, bad
args raise), ``measure_frame_data`` with a ~5 px and a ~6.7 px solve error
(measures OK at the true star, refinement + offset recorded on
``wcs_refined``/``pick_offset``; the unrefined fixed-pixel control still skips
non-positive-flux; no-star frames still skip non-positive-flux),
``measure_project`` / ``RunController.run_session`` over a session whose stored
WCS is uniformly ~5 px off (all frames measured + refined, ``refined_frames``
on the reports, ``wcs_refined_frames`` persisted in ``project.json``), and the
fixed-pixel fallback path staying unrefined.
"""

import io
import math
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits as pyfits
from astropy.time import Time

from seestar_controller import photometry
from seestar_controller.photometry import (
    ApertureParams,
    StarPickSpec,
    measure_frame,
    measure_frame_data,
    measure_project,
    pixel_to_sky,
    refine_pick_on_frame,
    resolve_pick_pixels,
    sky_to_pixel,
    try_sky_to_pixel,
)
from seestar_controller.photometry.measure import (
    REPROJECT_CENTROID_RADIUS_PX,
    REPROJECT_SEARCH_RADIUS_PX,
    FrameMeasurement,
    FrameSkippedError,
    MeasurementError,
    MeasureReport,
    SkippedFrame,
    StarMeasurement,
    _normalize_picks,
)
from seestar_controller.photometry.run import RunController, RunReport
from seestar_controller.store import (
    KIND_SUB,
    StarPick,
    create_project,
    lightcurve,
    load_star_picks,
    save_frame_wcs,
    save_star_picks,
    start_session,
    store_frame,
)
from seestar_controller.store.wcs import FrameWcs, load_frame_wcs

#: Directory of the photometry package (source-scan audit).
PHOTOMETRY_PACKAGE_DIR = Path(photometry.__file__).resolve().parent

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)

# --- synthetic drifting field -------------------------------------------------
#
# 192x192 canvas so the whole field survives the per-frame drift. The drift
# (20, 15) px/frame moves every star >= 25 px away from its fixed pick pixel
# already in frame 1 — with a noiseless frame the fixed pixel then collects
# exactly zero flux (the Gaussian tail underflows float32 against the 100 ADU
# background), so pixel-only picks deterministically SKIP the drifted frames.

SIZE = 192
BACKGROUND = 100.0
NOISE = 5.0
F0 = 20000.0  # constant comp peak flux
#: Star positions as (cy, cx) — row, column — on the reference frame (shift 0).
TARGET_POS = (60, 60)
COMP_POSITIONS = ((80, 90), (95, 35), (20, 75))

#: The aperture used everywhere (the defaults).
APERTURE = ApertureParams(radius_px=4.0, sky_in_px=8.0, sky_out_px=12.0)

#: The sky center of the reference WCS.
CRVAL = (83.0, 22.0)


def wcs_cards(shift_x: float = 0.0, shift_y: float = 0.0) -> dict:
    """A plain-dict FITS header with a simple CAR WCS whose reference pixel
    (1-based CRPIX) is shifted by ``(shift_x, shift_y)`` — the per-frame drift
    of the synthetic session (field rotation + tracking drift)."""
    from astropy.wcs import WCS

    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---CAR", "DEC--CAR"]
    w.wcs.crval = [CRVAL[0], CRVAL[1]]
    w.wcs.crpix = [97.0 + shift_x, 97.0 + shift_y]
    w.wcs.cdelt = [-0.001, 0.001]
    return dict(w.to_header())


#: The reference WCS (shift 0) — the frame the picks were made on.
REF_CARDS = wcs_cards(0.0, 0.0)


def _coord(pos: tuple[float, float]) -> tuple[float, float]:
    c = pixel_to_sky(REF_CARDS, pos[0], pos[1])  # (x, y) = (col, row)
    return (c.ra_deg, c.dec_deg)


#: The sky coordinates of the synthetic stars on the reference frame.
STAR_COORDS: dict[str, tuple[float, float]] = {
    "target": _coord((TARGET_POS[1], TARGET_POS[0])),
    "comp_a": _coord((COMP_POSITIONS[0][1], COMP_POSITIONS[0][0])),
    "comp_b": _coord((COMP_POSITIONS[1][1], COMP_POSITIONS[1][0])),
    "comp_c": _coord((COMP_POSITIONS[2][1], COMP_POSITIONS[2][0])),
}


def star_pixel(shift_x: float, shift_y: float, coord: tuple[float, float]) -> tuple[float, float]:
    """The 0-based ``(x, y)`` pixel of the sky ``coord`` in the frame whose WCS
    is shifted by ``(shift_x, shift_y)`` — computed through the same WCS the
    run will use."""
    return sky_to_pixel(wcs_cards(shift_x, shift_y), coord)


def drift(i: int) -> tuple[float, float]:
    """The WCS shift of frame ``i``: (20, 15) px per frame (alt-az field
    rotation + tracking drift over the night). Frame 0 has no shift."""
    return (20.0 * i, 15.0 * i)


def date_obs_str(i: int, seconds: int = 10) -> str:
    """The DATE-OBS of frame ``i``: T0 + i*seconds, naive ISOT (UTC)."""
    instant = T0 + timedelta(seconds=seconds * i)
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "")


def expected_jd(i: int) -> float:
    return float(Time(date_obs_str(i), format="isot", scale="utc").jd)


def frame_data(
    star_positions: dict[str, tuple[float, float]],
    target_peak: float,
    seed: int,
    *,
    noise: float = NOISE,
    sigma2: float = 8.0,
) -> np.ndarray:
    """A synthetic SIZE x SIZE frame: flat background + Gaussian stars
    (sigma^2=``sigma2``) at the GIVEN ``(x, y)`` positions (``star_positions``
    maps the star key to its 0-based ``(x, y)`` — the reprojected position for
    this frame) + noise, float32. The target sits at
    ``star_positions["target"]`` with ``target_peak``, the comps at their
    positions with peak ``F0``."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    data = np.full((SIZE, SIZE), BACKGROUND, dtype=np.float64)
    peaks = {"target": target_peak}
    for key in ("comp_a", "comp_b", "comp_c"):
        peaks[key] = F0
    for key, peak in peaks.items():
        x, y = star_positions[key]
        data = data + peak * np.exp(-(((yy - y) ** 2 + (xx - x) ** 2) / sigma2))
    if noise:
        data = data + rng.normal(0.0, noise, size=(SIZE, SIZE))
    return np.ascontiguousarray(data, dtype=np.float32)


def drifted_positions(shift_x: float, shift_y: float) -> dict[str, tuple[float, float]]:
    """The star pixel positions of the frame with the given WCS shift."""
    return {key: star_pixel(shift_x, shift_y, coord) for key, coord in STAR_COORDS.items()}


def offset_positions(
    shift_x: float, shift_y: float, dx: float, dy: float = 0.0
) -> dict[str, tuple[float, float]]:
    """The star pixel positions of the frame with the given WCS shift, but
    DRAWN ``(dx, dy)`` px away from where the WCS predicts them — the
    ~few-px plate-solve pointing error of issue 12 (the stored WCS says the
    star is at the drifted position, the frame data has it offset)."""
    return {
        key: (pos[0] + dx, pos[1] + dy)
        for key, pos in drifted_positions(shift_x, shift_y).items()
    }


def fits_bytes(data: np.ndarray, *, date_obs: str | None = "2026-11-20T22:00:00.000") -> bytes:
    """A small valid FITS file with the given data (+ DATE-OBS unless None)."""
    hdu = pyfits.PrimaryHDU(data=np.asarray(data, dtype=np.float32))
    if date_obs is not None:
        hdu.header["DATE-OBS"] = date_obs
    hdu.header["EXPTIME"] = 10.0
    hdu.header["GAIN"] = 50
    hdu.header["OBJECT"] = "VW Cep"
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def make_picks(*, with_sky: bool = True) -> list[StarPick]:
    """One target + 3 comps as STORE StarPick records: the fixed pixel is the
    reference-frame position (where the pick was made), the sky coordinate is
    the star's J2000 position (None when ``with_sky=False`` — pixel-only
    picks)."""
    roles = ("target", "comp", "comp", "comp")
    keys = ("target", "comp_a", "comp_b", "comp_c")
    base = (
        (TARGET_POS[1], TARGET_POS[0]),
        COMP_POSITIONS[0][::-1],
        COMP_POSITIONS[1][::-1],
        COMP_POSITIONS[2][::-1],
    )
    picks = []
    for role, key, (bx, by) in zip(roles, keys, base):
        ra, dec = STAR_COORDS[key] if with_sky else (None, None)
        picks.append(
            StarPick(
                role=role,
                label=key,
                x=bx,
                y=by,
                ra_deg=ra,
                dec_deg=dec,
                aperture_radius=4.0,
                sky_annulus_inner=8.0,
                sky_annulus_outer=12.0,
            )
        )
    return picks


def make_spec_picks(*, with_sky: bool = True) -> list[StarPickSpec]:
    """The same picks as :class:`StarPickSpec` objects (the picker session's
    shape — ``coord`` instead of ``ra_deg``/``dec_deg``)."""
    from seestar_controller.catalog.coords import J2000Coord

    specs = []
    for pick in make_picks(with_sky=with_sky):
        coord = (
            J2000Coord(ra_deg=pick.ra_deg, dec_deg=pick.dec_deg)
            if pick.ra_deg is not None and pick.dec_deg is not None
            else None
        )
        specs.append(StarPickSpec(role=pick.role, x=pick.x, y=pick.y, coord=coord, label=pick.label))
    return specs


def frozen(dt: datetime):
    return lambda: dt


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    p = create_project("VW Cep night", root=root)
    yield p
    p.close()


def make_drifting_session(project, n: int = 5, amp: float = 0.3, *, noise: float = NOISE):
    """One session of ``n`` sub-frames. Frame ``i`` has its own solved WCS
    (``save_frame_wcs`` with the drifted CARDS) and the stars drawn at the
    position their sky coordinate maps to in THAT frame. Returns
    ``(session_id, frames, injected_mags)``."""
    session = start_session(project, clock=frozen(T0))
    injected: list[float] = []
    frames = []
    for i in range(n):
        m_inj = amp * math.sin(2.0 * math.pi * i / n)
        injected.append(m_inj)
        target_peak = F0 * 10 ** (-0.4 * m_inj)
        sx, sy = drift(i)
        positions = drifted_positions(sx, sy)
        frame = store_frame(
            project,
            session,
            KIND_SUB,
            fits_bytes(
                frame_data(positions, target_peak, seed=100 + i, noise=noise),
                date_obs=date_obs_str(i),
            ),
        )
        save_frame_wcs(
            project,
            frame.id,
            solver="gaia",
            n_stars=12,
            center_ra_deg=CRVAL[0],
            center_dec_deg=CRVAL[1],
            cards=wcs_cards(sx, sy),
            rms_arcsec=0.3,
        )
        frames.append(frame)
    return session, frames, injected


# --- try_sky_to_pixel: the lenient helper ----------------------------------------


def test_try_sky_to_pixel_roundtrip_and_lenient():
    # a WCS header: the exact inverse of pixel_to_sky (0-based pixels)
    coord = pixel_to_sky(REF_CARDS, 60.0, 50.0)  # (x, y) — NOT symmetric
    assert try_sky_to_pixel(REF_CARDS, (coord.ra_deg, coord.dec_deg)) == pytest.approx((60.0, 50.0))
    x, y = sky_to_pixel(wcs_cards(20.0, 15.0), (coord.ra_deg, coord.dec_deg))
    assert try_sky_to_pixel(wcs_cards(20.0, 15.0), coord) == pytest.approx((x, y))
    # no WCS in the header (a raw Seestar sub) -> None, never raises
    assert try_sky_to_pixel({"DATE-OBS": "2026-11-20T22:00:00"}, coord) is None
    assert try_sky_to_pixel(None, coord) is None
    # a bad coord -> None, never raises
    assert try_sky_to_pixel(REF_CARDS, "not-a-coord") is None  # type: ignore[arg-type]


# --- resolve_pick_pixels: reprojection + per-pick fallback -------------------------


def test_resolve_pick_pixels_reprojects_sky_picks_and_falls_back_per_pick():
    norm = _normalize_picks(make_spec_picks(), None)
    assert all(p.ra_deg is not None and p.dec_deg is not None for p in norm)
    # no WCS -> unchanged, count 0
    same, count = resolve_pick_pixels(norm, None)
    assert count == 0 and same == norm
    # a shifted frame WCS -> every sky pick reprojected to the shifted pixel
    shifted, count = resolve_pick_pixels(norm, wcs_cards(20.0, 15.0))
    assert count == len(norm)
    for orig, res in zip(norm, shifted):
        expected = sky_to_pixel(wcs_cards(20.0, 15.0), (orig.ra_deg, orig.dec_deg))
        assert (res.x, res.y) == pytest.approx(expected)
        assert res.role == orig.role and res.aperture == orig.aperture
    # a pixel-only pick mixed in keeps its fixed pixel (count only the sky picks)
    specs = make_spec_picks()
    specs[1] = StarPickSpec(role="comp", x=16.0, y=16.0, coord=None, label="pixel-only")
    norm_mixed = _normalize_picks(specs, None)
    resolved, count = resolve_pick_pixels(norm_mixed, wcs_cards(20.0, 15.0))
    assert count == len(norm_mixed) - 1
    assert (resolved[1].x, resolved[1].y) == (16.0, 16.0)  # the fallback pick
    # a FrameWcs record and an astropy WCS (to_wcs()) work as the wcs argument too
    record = FrameWcs(
        frame_id=9, session_id="s", solver="gaia", solved_at="t", rms_arcsec=0.3,
        n_stars=12, center_ra_deg=CRVAL[0], center_dec_deg=CRVAL[1],
        cards=wcs_cards(20.0, 15.0),
    )
    for wcs in (record, record.to_wcs()):
        res, cnt = resolve_pick_pixels(norm, wcs)
        assert cnt == len(norm)
        assert (res[0].x, res[0].y) == pytest.approx(
            sky_to_pixel(record.cards, (norm[0].ra_deg, norm[0].dec_deg))
        )
    # garbage wcs -> MeasurementError
    with pytest.raises(MeasurementError, match="wcs must be None"):
        resolve_pick_pixels(norm, 42)  # type: ignore[arg-type]


# --- measure_frame_data with wcs=: reprojection, fallback, skip ---------------------


def test_measure_frame_data_reprojects_sky_picks_onto_the_frame_wcs():
    # frame 1 of the drifting night: stars at the SHIFTED positions, no noise
    # (the fixed pick pixel collects exactly zero flux -> deterministic skip)
    sx, sy = drift(1)
    positions = drifted_positions(sx, sy)
    m_inj = 0.25
    data = frame_data(positions, F0 * 10 ** (-0.4 * m_inj), seed=7, noise=0.0)
    header = {"DATE-OBS": date_obs_str(1)}
    picks = make_spec_picks()
    base_target = (TARGET_POS[1], TARGET_POS[0])

    # WITHOUT the frame WCS the fixed pixel pick misses the drifted star
    with pytest.raises(FrameSkippedError) as excinfo:
        measure_frame_data(data, header, picks, APERTURE)
    assert excinfo.value.kind == "non-positive-flux"

    # WITH the frame WCS every sky pick is reprojected and measured there
    fm = measure_frame_data(data, header, picks, APERTURE, wcs=wcs_cards(sx, sy))
    assert isinstance(fm, FrameMeasurement)
    assert fm.wcs_reprojected == 4
    # issue 12: the reprojected picks are centroid-refined on the (noiseless)
    # frame — the star sits exactly at the WCS pixel, so the refinement is
    # the identity and the measured pixel is the reprojected one
    assert fm.wcs_refined == 4
    assert fm.target.pick_offset == pytest.approx((0.0, 0.0), abs=1e-6)
    assert fm.mag == pytest.approx(m_inj, abs=0.02)
    expected_target = star_pixel(sx, sy, STAR_COORDS["target"])
    assert (fm.target.x, fm.target.y) == pytest.approx(expected_target, abs=1e-6)
    assert (fm.target.x, fm.target.y) != pytest.approx(base_target)  # not the fixed pick
    assert fm.jd == pytest.approx(expected_jd(1), abs=1e-9)

    # a FrameWcs record and an astropy WCS (to_wcs()) work as wcs= too
    record = FrameWcs(
        frame_id=9, session_id="s", solver="gaia", solved_at="t", rms_arcsec=0.3,
        n_stars=12, center_ra_deg=CRVAL[0], center_dec_deg=CRVAL[1], cards=wcs_cards(sx, sy),
    )
    fm2 = measure_frame_data(data, header, picks, APERTURE, wcs=record)
    fm3 = measure_frame_data(data, header, picks, APERTURE, wcs=record.to_wcs())
    assert fm2.wcs_reprojected == 4 and fm2.mag == pytest.approx(fm.mag)
    assert fm3.wcs_reprojected == 4 and fm3.mag == pytest.approx(fm.mag)

    # pixel-only picks (no sky coords) + a WCS -> fixed pixels, count 0
    # (checked on the UNDRIFTED frame, where the fixed pick hits the star)
    data0 = frame_data(drifted_positions(0.0, 0.0), F0, seed=3, noise=0.0)
    fm4 = measure_frame_data(
        data0, {"DATE-OBS": date_obs_str(0)}, make_spec_picks(with_sky=False), APERTURE,
        wcs=wcs_cards(0.0, 0.0),
    )
    assert fm4.wcs_reprojected == 0
    assert fm4.wcs_refined == 0  # issue 12: fixed-pixel picks are never refined
    assert fm4.target.pick_offset is None
    assert (fm4.target.x, fm4.target.y) == pytest.approx(base_target)
    # ... while on the DRIFTED frame the same pixel-only picks miss the star
    with pytest.raises(FrameSkippedError) as excinfo2:
        measure_frame_data(data, header, make_spec_picks(with_sky=False), APERTURE, wcs=wcs_cards(sx, sy))
    assert excinfo2.value.kind == "non-positive-flux"


def test_measure_frame_data_reprojected_pick_outside_image_skips_the_frame():
    # a huge drift: the target's sky coord maps far outside the image
    sx, sy = 500.0, 0.0
    data = frame_data(drifted_positions(sx, sy), F0, seed=8, noise=0.0)
    header = {"DATE-OBS": date_obs_str(0)}
    with pytest.raises(FrameSkippedError) as excinfo:
        measure_frame_data(data, header, make_spec_picks(), APERTURE, wcs=wcs_cards(sx, sy))
    assert excinfo.value.kind == "outside-image"
    assert "outside" in str(excinfo.value)


# --- measure_frame / measure_project on stored frames with per-frame WCS -----------


def test_measure_frame_uses_the_stored_wcs_of_the_frame(project):
    session = start_session(project, clock=frozen(T0))
    sx, sy = drift(1)
    positions = drifted_positions(sx, sy)
    m_inj = 0.3
    frame = store_frame(
        project,
        session,
        KIND_SUB,
        fits_bytes(
            frame_data(positions, F0 * 10 ** (-0.4 * m_inj), seed=55, noise=0.0),
            date_obs=date_obs_str(0),
        ),
    )
    save_frame_wcs(
        project, frame.id, solver="gaia", n_stars=12,
        center_ra_deg=CRVAL[0], center_dec_deg=CRVAL[1], cards=wcs_cards(sx, sy),
    )
    # the stored WCS is loaded by measure_frame itself
    assert load_frame_wcs(project, frame.id) is not None
    save_star_picks(project, make_picks())
    meas = measure_frame(project, frame.id, load_star_picks(project))
    assert meas.mag == pytest.approx(m_inj, abs=0.02)  # measured at the reprojected pixel
    # the same frame with PIXEL-ONLY picks misses the drifted star (zero flux)
    save_star_picks(project, make_picks(with_sky=False))
    with pytest.raises(FrameSkippedError) as excinfo:
        measure_frame(project, frame.id, load_star_picks(project))
    assert excinfo.value.kind == "non-positive-flux"


def test_measure_project_recovers_all_drifted_frames_via_per_frame_wcs(project):
    n = 5
    _session, _frames, injected = make_drifting_session(project, n=n)
    save_star_picks(project, make_picks())  # pixel + sky picks

    report = measure_project(project, load_star_picks(project))
    assert isinstance(report, MeasureReport)
    assert report.frames == n
    assert len(report.measurements) == n  # ALL drifted frames measured
    assert report.skipped == ()
    assert report.skip_reasons == {}
    assert report.skip_summary == "no frames skipped"
    # issue 09 aggregation: every measured frame was WCS-reprojected
    assert report.wcs_frames == n
    assert report.pixel_frames == 0
    assert all(fm.wcs_reprojected == 4 for fm in report.frame_results)
    # the per-frame target pixel follows the drift (not the fixed pick);
    # issue 12: the pick is centroid-refined on the frame, so it lands within
    # ~1 px of the (noisy-frame) star centroid rather than exactly on the WCS
    # pixel — the drift itself is >= 25 px, so abs=1 still proves the drift
    for i, fm in enumerate(report.frame_results):
        expected = star_pixel(*drift(i), STAR_COORDS["target"])
        assert (fm.target.x, fm.target.y) == pytest.approx(expected, abs=1.0)
        # issue 12: every reprojected pick was centroid-refined on the frame
        assert fm.wcs_refined == 4
        assert fm.target.pick_offset is not None
        assert math.hypot(*fm.target.pick_offset) <= REPROJECT_SEARCH_RADIUS_PX
    # the injected sinusoid is recovered on every frame
    lc = lightcurve(project)
    assert len(lc) == n
    for meas, m_inj, i in zip(lc, injected, range(n)):
        assert meas.mag == pytest.approx(m_inj, abs=0.02)
        assert meas.jd == pytest.approx(expected_jd(i), abs=1e-9)


def test_measure_project_pixel_only_picks_skip_the_drifted_frames(project):
    """The regression proof: on the same drifting session, pixel-only picks
    (no ra/dec) only work for frame 0 — every drifted frame FAILS with the
    fixed pixel: the star is >= 25 px away, so the fixed pixel collects only
    photometric dust (flux ~1e-6 instead of ~20000): the frame is either
    skipped (non-positive flux) or measured with a garbage flux — never the
    star. (The dust sign is a photutils rounding artifact, so the assertion is
    on the flux magnitude, not the skip/measure branch.)"""
    n = 5
    _session, frames, injected = make_drifting_session(project, n=n, noise=0.0)
    save_star_picks(project, make_picks(with_sky=False))  # pixel-only picks

    report = measure_project(project, load_star_picks(project))
    assert report.wcs_frames == 0  # nothing was reprojected (no sky coords)
    assert report.pixel_frames == len(report.measurements)
    # frame 0 (no drift): the fixed pick hits the star, correct magnitude
    m0 = next((m for m in report.measurements if m.frame_id == frames[0].id), None)
    assert m0 is not None
    assert m0.mag == pytest.approx(injected[0], abs=0.02)
    # every drifted frame: skipped (non-positive dust flux) or measured dust —
    # never the ~20000 ADU star
    measured = {m.frame_id: fm for m, fm in zip(report.measurements, report.frame_results)}
    for i in range(1, n):
        fid = frames[i].id
        if fid in measured:
            assert abs(measured[fid].target.flux) < 1000.0
        else:
            skip = next(s for s in report.skipped if s.frame_id == fid)
            assert skip.kind == "non-positive-flux"
    assert set(report.skip_reasons) <= {"non-positive-flux"}
    assert report.skip_reasons == {k: v for k, v in report.skip_reasons.items()}


# --- RunController.run_session: per-frame WCS, fallback, aggregated report ----------


def test_run_session_reprojects_sky_picks_per_frame_and_reports_modes(project):
    n = 5
    session, frames, injected = make_drifting_session(project, n=n)
    # one extra sub WITHOUT a stored WCS: the star sits at the FIXED pick
    # pixel, so the fixed-pixel fallback must measure it
    no_wcs_frame = store_frame(
        project,
        session,
        KIND_SUB,
        fits_bytes(
            frame_data(drifted_positions(0.0, 0.0), F0, seed=900, noise=0.0),
            date_obs=date_obs_str(50),
        ),
    )
    assert load_frame_wcs(project, no_wcs_frame.id) is None
    # one extra solved sub whose drift pushes the target OUTSIDE the image
    out_frame = store_frame(
        project,
        session,
        KIND_SUB,
        fits_bytes(
            frame_data(drifted_positions(500.0, 0.0), F0, seed=901, noise=0.0),
            date_obs=date_obs_str(51),
        ),
    )
    save_frame_wcs(
        project, out_frame.id, solver="gaia", n_stars=12,
        center_ra_deg=CRVAL[0], center_dec_deg=CRVAL[1], cards=wcs_cards(500.0, 0.0),
    )
    # one extra solved all-zero sub -> non-positive flux skip
    zero_frame = store_frame(
        project,
        session,
        KIND_SUB,
        fits_bytes(np.zeros((SIZE, SIZE), dtype=np.float32), date_obs=date_obs_str(52)),
    )
    save_frame_wcs(
        project, zero_frame.id, solver="gaia", n_stars=12,
        center_ra_deg=CRVAL[0], center_dec_deg=CRVAL[1], cards=wcs_cards(0.0, 0.0),
    )
    save_star_picks(project, make_picks())

    ctrl = RunController(project=project)
    report = ctrl.run_session(session)
    assert isinstance(report, RunReport)
    # 5 drifted (WCS) + 1 unsolved (fixed-pixel fallback) measured; 2 skipped
    assert len(report.measurements) == n + 1
    assert [s.frame_id for s in report.skipped] == [out_frame.id, zero_frame.id]
    assert report.skipped[0].kind == "outside-image"
    assert "outside" in report.skipped[0].reason
    assert report.skipped[1].kind == "non-positive-flux"
    assert "non-positive" in report.skipped[1].reason
    # issue 09 aggregation on the RunReport
    assert report.skip_reasons == {"outside-image": 1, "non-positive-flux": 1}
    assert report.skip_summary == "2 frames skipped (non-positive-flux×1, outside-image×1)"
    assert report.wcs_frames == n  # the solved drifting frames
    assert report.pixel_frames == 1  # the unsolved frame, fixed pixel pick
    assert report.wcs_summary == (
        f"{n} of {n + 1} frames via per-frame WCS reprojection (1 fixed-pixel)"
    )
    assert report.frames == n + 3
    # every measured frame recovered its injected mag (incl. the fallback one)
    by_frame = {m.frame_id: m for m in report.measurements}
    for i, frame in enumerate(frames):
        assert by_frame[frame.id].mag == pytest.approx(injected[i], abs=0.02)
    assert by_frame[no_wcs_frame.id].mag == pytest.approx(0.0, abs=0.02)
    # the per-frame reprojection counts ride on the frame_results
    assert [fm.wcs_reprojected for fm in report.frame_results[:n]] == [4] * n
    assert report.frame_results[n].wcs_reprojected == 0  # the fallback frame
    # ... and are persisted into project.json's last_run record
    last = project.config["photometry"]["last_run"]
    assert last["session_id"] == session
    assert last["measured"] == n + 1 and last["skipped"] == 2
    assert last["skip_reasons"] == {"outside-image": 1, "non-positive-flux": 1}
    assert last["wcs_frames"] == n and last["pixel_frames"] == 1
    assert len(lightcurve(project)) == n + 1


def test_run_session_with_picker_spec_picks_also_reprojects(project):
    """The picker controller's StarPickSpec picks (``coord``) are reprojected
    too — not only the stored StarPick records."""
    from seestar_controller.photometry import PickController

    session, _frames, injected = make_drifting_session(project, n=4)

    class FakeImage:
        def __init__(self, data, header):
            self.data = data
            self.header = header

        @property
        def width(self):
            return SIZE

        @property
        def height(self):
            return SIZE

    picker = PickController(project=project)
    picker.set_image(FakeImage(frame_data(drifted_positions(0.0, 0.0), F0, seed=1), REF_CARDS))
    # pick on the reference frame: the picks get pixel + sky coord
    picker.pick_at(TARGET_POS[1], TARGET_POS[0], "target", snap=False)
    for cy, cx in COMP_POSITIONS:
        picker.pick_at(cx, cy, "comp", snap=False)
    assert all(p.coord is not None for p in picker.picks)
    ctrl = RunController(picker_controller=picker)
    report = ctrl.run_session(session)
    assert len(report.measurements) == 4 and report.skipped == ()
    assert report.wcs_frames == 4 and report.pixel_frames == 0
    for meas, m_inj in zip(report.measurements, injected):
        assert meas.mag == pytest.approx(m_inj, abs=0.02)


# --- pure report aggregation properties ---------------------------------------------


def _fm(wcs_reprojected: int = 0, jd: float = 2460000.5, wcs_refined: int = 0) -> FrameMeasurement:
    star = StarMeasurement(
        role="target", label=None, x=1.0, y=1.0, flux=100.0, flux_err=1.0, mag=-5.0, mag_err=0.01
    )
    return FrameMeasurement(
        jd=jd, target=star, comps=(star,), ensemble_mag=-5.0, mag=0.0, mag_err=0.02,
        wcs_reprojected=wcs_reprojected, wcs_refined=wcs_refined,
    )


def test_run_report_skip_and_wcs_aggregation_pure():
    report = RunReport(
        measurements=(),
        skipped=(
            SkippedFrame(frame_id=1, reason="r1", kind="non-positive-flux"),
            SkippedFrame(frame_id=2, reason="r2", kind="non-positive-flux"),
            SkippedFrame(frame_id=3, reason="r3", kind="outside-image"),
            SkippedFrame(frame_id=4, reason="r4"),  # no kind -> "error"
        ),
        session_id="s1",
        frame_results=(_fm(4, wcs_refined=4), _fm(0), _fm(2, wcs_refined=1)),
    )
    assert report.skip_reasons == {
        "non-positive-flux": 2,
        "outside-image": 1,
        "error": 1,
    }
    assert report.skip_summary == "4 frames skipped (error×1, non-positive-flux×2, outside-image×1)"
    assert report.wcs_frames == 2
    assert report.pixel_frames == 1
    assert report.refined_frames == 2  # issue 12
    assert report.wcs_summary == "2 of 3 frames via per-frame WCS reprojection (1 fixed-pixel)"
    # empty report
    empty = RunReport(measurements=(), skipped=(), session_id="s")
    assert empty.skip_reasons == {}
    assert empty.skip_summary == "no frames skipped"
    assert empty.wcs_frames == 0 and empty.pixel_frames == 0
    assert empty.refined_frames == 0  # issue 12
    assert empty.wcs_summary == "no frames measured"
    # one frame, all WCS
    one = RunReport(measurements=(), skipped=(), session_id="s", frame_results=(_fm(4),))
    assert one.wcs_summary == "1 of 1 frame via per-frame WCS reprojection"
    assert one.refined_frames == 0  # reprojected but not refined


def test_measure_report_skip_and_wcs_aggregation_pure():
    report = MeasureReport(
        measurements=(),
        skipped=(SkippedFrame(frame_id=7, reason="boom", kind="unreadable-frame"),),
        frame_results=(_fm(0),),
    )
    assert report.skip_reasons == {"unreadable-frame": 1}
    assert report.skip_summary == "1 frame skipped (unreadable-frame×1)"
    assert report.wcs_frames == 0 and report.pixel_frames == 1
    assert report.refined_frames == 0  # issue 12
    assert MeasureReport(measurements=(), skipped=()).skip_summary == "no frames skipped"
    # issue 12: the refined-frame aggregation
    refined = MeasureReport(
        measurements=(),
        skipped=(),
        frame_results=(_fm(4, wcs_refined=4), _fm(4, wcs_refined=2), _fm(0)),
    )
    assert refined.wcs_frames == 2 and refined.pixel_frames == 1
    assert refined.refined_frames == 2


def test_error_kinds_are_carried_on_the_exceptions():
    with pytest.raises(FrameSkippedError) as excinfo:
        measure_frame_data(
            np.zeros((SIZE, SIZE), dtype=np.float32),
            {"DATE-OBS": date_obs_str(0)},
            make_spec_picks(),
            APERTURE,
            wcs=wcs_cards(0.0, 0.0),
        )
    assert excinfo.value.kind == "non-positive-flux"
    assert isinstance(excinfo.value, MeasurementError)
    with pytest.raises(MeasurementError, match="wcs must be None"):
        measure_frame_data(
            np.zeros((SIZE, SIZE), dtype=np.float32),
            {"DATE-OBS": date_obs_str(0)},
            make_spec_picks(),
            APERTURE,
            wcs=object(),
        )


# --- issue 12: centroid refinement of WCS-reprojected picks on the frame -------
#
# The stored solve WCS has a ~few-px pointing error: on the live vw-cep run the
# true star core sits 4.5-6.7 px from the WCS-predicted pixel on every frame,
# so with aperture_radius=4 the core falls OUTSIDE the aperture while the
# 8-12 px sky annulus catches the star wings -> background overestimated ->
# non-positive flux -> spurious skip on nearly every frame. After the issue-09
# reprojection each pick is therefore centroid-refined on the actual frame.


def test_refine_pick_on_frame_centroids_an_offset_star_and_rejects_no_star():
    # the defaults: the centroid window is smaller than the search window
    assert 0.0 < REPROJECT_CENTROID_RADIUS_PX < REPROJECT_SEARCH_RADIUS_PX
    sx, sy = drift(1)
    base = drifted_positions(sx, sy)
    pred = base["target"]
    # the star is drawn 5 px away from the WCS-predicted pixel (issue 12)
    drawn = offset_positions(sx, sy, 5.0, 0.0)
    data = frame_data(drawn, F0, seed=21, noise=0.0)
    refined = refine_pick_on_frame(data, *pred)
    assert refined is not None
    assert refined == pytest.approx(drawn["target"], abs=0.5)
    # the refinement stays within the search window of the predicted pixel
    assert math.hypot(refined[0] - pred[0], refined[1] - pred[1]) <= REPROJECT_SEARCH_RADIUS_PX
    # every star of the field is found the same way
    for key, pos in drawn.items():
        assert refine_pick_on_frame(data, *base[key]) == pytest.approx(pos, abs=0.5)
    # a no-star frame (flat background + noise): no significant local max -> None
    rng = np.random.default_rng(31)
    flat = np.ascontiguousarray(
        np.full((SIZE, SIZE), BACKGROUND) + rng.normal(0.0, NOISE, (SIZE, SIZE)),
        dtype=np.float32,
    )
    assert refine_pick_on_frame(flat, *pred) is None
    # an all-zero frame -> None as well
    assert refine_pick_on_frame(np.zeros((SIZE, SIZE), dtype=np.float32), *pred) is None
    # a star far OUTSIDE the search window (sharp profile, 15 px away) -> None
    far = offset_positions(sx, sy, 15.0, 0.0)
    far_data = frame_data(far, F0, seed=32, noise=0.0, sigma2=2.0)
    assert refine_pick_on_frame(far_data, *pred) is None
    # a predicted pixel outside the image -> None (never raises)
    assert refine_pick_on_frame(data, -440.0, 10.0) is None
    assert refine_pick_on_frame(data, 10.0, 99999.0) is None
    # bad arguments raise MeasurementError
    with pytest.raises(MeasurementError, match="must be positive"):
        refine_pick_on_frame(data, *pred, search_radius_px=0.0)
    with pytest.raises(MeasurementError, match="must be positive"):
        refine_pick_on_frame(data, *pred, centroid_radius_px=-1.0)
    with pytest.raises(MeasurementError, match="must be positive"):
        refine_pick_on_frame(data, *pred, significance_sigma=0.0)
    with pytest.raises(MeasurementError, match="must be finite"):
        refine_pick_on_frame(data, float("nan"), 0.0)  # type: ignore[arg-type]


def _fixed_picks_at(positions: dict[str, tuple[float, float]]) -> list[StarPickSpec]:
    """Pixel-only picks (no sky coord) placed at the GIVEN ``(x, y)``
    positions (keyed by label) — the fixed-pixel fallback path of issue 09,
    which issue 12 deliberately leaves unchanged (never refined)."""
    picks: list[StarPickSpec] = []
    for spec in make_spec_picks(with_sky=False):
        label = spec.label
        assert label is not None  # make_spec_picks always labels its picks
        x, y = positions[label]
        picks.append(StarPickSpec(role=spec.role, x=x, y=y, coord=None, label=label))
    return picks


def test_measure_frame_data_centroid_refines_5px_offset_reprojected_picks():
    """The issue-12 headline: a synthetic frame whose stars sit ~5 px from the
    WCS-predicted pixel (the live plate-solve pointing error) measures OK —
    after the reprojection the picks are centroid-refined onto the stars."""
    sx, sy = drift(1)
    wcs = wcs_cards(sx, sy)
    predicted = drifted_positions(sx, sy)
    drawn = offset_positions(sx, sy, 5.0, 0.0)  # the ~5 px solve error
    m_inj = 0.25
    data = frame_data(drawn, F0 * 10 ** (-0.4 * m_inj), seed=41, noise=0.0)
    header = {"DATE-OBS": date_obs_str(1)}

    fm = measure_frame_data(data, header, make_spec_picks(), APERTURE, wcs=wcs)
    assert fm.wcs_reprojected == 4
    assert fm.wcs_refined == 4  # every reprojected pick was refined
    assert fm.mag == pytest.approx(m_inj, abs=0.02)
    # the target was measured AT THE STAR (5 px from the WCS-predicted pixel),
    # and the refinement offset is recorded on the StarMeasurement
    assert (fm.target.x, fm.target.y) == pytest.approx(drawn["target"], abs=0.5)
    assert fm.target.pick_offset == pytest.approx((5.0, 0.0), abs=0.5)
    for comp, key in zip(fm.comps, ("comp_a", "comp_b", "comp_c")):
        assert (comp.x, comp.y) == pytest.approx(drawn[key], abs=0.5)
        assert comp.pick_offset == pytest.approx((5.0, 0.0), abs=0.5)

    # control — the fixed-pixel fallback path is UNCHANGED (never refined):
    # pixel-only picks at the WCS-predicted pixels, no wcs: the star is
    # measured at the predicted pixel, with no refinement and no offset
    fm_fixed = measure_frame_data(data, header, _fixed_picks_at(predicted), APERTURE)
    assert fm_fixed.wcs_reprojected == 0 and fm_fixed.wcs_refined == 0
    assert fm_fixed.target.pick_offset is None
    assert (fm_fixed.target.x, fm_fixed.target.y) == pytest.approx(predicted["target"])


def test_measure_frame_data_refinement_recovers_the_live_non_positive_flux_skip():
    """The exact live failure mode of issue 12: with a sharper (saturated-like)
    profile the star core 6.7 px from the WCS-predicted pixel falls outside the
    r=4 aperture while the 8-12 px annulus catches the wings -> the UNREFINED
    pick skips non-positive-flux; the centroid-refined one measures OK."""
    sx, sy = drift(1)
    wcs = wcs_cards(sx, sy)
    predicted = drifted_positions(sx, sy)
    drawn = offset_positions(sx, sy, 6.7, 0.0)
    m_inj = 0.25
    data = frame_data(drawn, F0 * 10 ** (-0.4 * m_inj), seed=42, noise=0.0, sigma2=4.0)
    header = {"DATE-OBS": date_obs_str(1)}

    # control: the same frame with pixel-only picks at the predicted pixels and
    # NO wcs (the fixed-pixel path — unchanged by issue 12) -> the star core is
    # outside the aperture, the annulus is contaminated by the wings -> skip
    with pytest.raises(FrameSkippedError) as excinfo:
        measure_frame_data(data, header, _fixed_picks_at(predicted), APERTURE)
    assert excinfo.value.kind == "non-positive-flux"

    # with the frame WCS the picks are reprojected AND centroid-refined -> OK
    fm = measure_frame_data(data, header, make_spec_picks(), APERTURE, wcs=wcs)
    assert fm.wcs_reprojected == 4 and fm.wcs_refined == 4
    assert fm.mag == pytest.approx(m_inj, abs=0.02)
    assert (fm.target.x, fm.target.y) == pytest.approx(drawn["target"], abs=0.5)
    assert fm.target.pick_offset == pytest.approx((6.7, 0.0), abs=0.5)


def test_measure_frame_data_no_star_frame_still_skips_non_positive_flux():
    """Issue 12 + issue 14: a no-star frame has no significant local max near
    the reprojected pick — the predicted pixel is kept; on the all-zero frame
    every pick's flux is exactly 0, so the TARGET pick is unusable and the
    frame still skips with kind non-positive-flux (carrying all 4 per-pick
    drops). On the noisy no-star frame the target's noise flux can be
    positive: then the frame is measured on whatever remains and the
    non-positive picks are dropped per pick (issue 14)."""
    sx, sy = drift(1)
    predicted = drifted_positions(sx, sy)
    rng = np.random.default_rng(51)
    data = np.ascontiguousarray(
        np.full((SIZE, SIZE), BACKGROUND) + rng.normal(0.0, NOISE, (SIZE, SIZE)),
        dtype=np.float32,
    )
    header = {"DATE-OBS": date_obs_str(1)}
    # no significant local maximum near the predicted target pixel
    assert refine_pick_on_frame(data, *predicted["target"]) is None
    # the noisy no-star frame: measured or skipped depending on the target's
    # noise flux — but never a crash, and the non-positive picks are recorded
    try:
        fm = measure_frame_data(data, header, make_spec_picks(), APERTURE, wcs=wcs_cards(sx, sy))
    except FrameSkippedError as exc:
        assert exc.kind == "non-positive-flux"
        assert any(d.role == "target" for d in exc.dropped_picks)
    else:
        assert fm.dropped_picks  # the non-positive comps were dropped per pick
        assert all(d.kind == "non-positive-flux" for d in fm.dropped_picks)
        assert abs(fm.target.flux) < 1000.0  # noise dust, never a star
    # ... and an all-zero frame always skips: the target pick is unusable
    with pytest.raises(FrameSkippedError) as excinfo2:
        measure_frame_data(
            np.zeros((SIZE, SIZE), dtype=np.float32),
            header,
            make_spec_picks(),
            APERTURE,
            wcs=wcs_cards(sx, sy),
        )
    assert excinfo2.value.kind == "non-positive-flux"
    assert "non-positive" in str(excinfo2.value)
    assert len(excinfo2.value.dropped_picks) == 4
    assert all(d.kind == "non-positive-flux" for d in excinfo2.value.dropped_picks)


def make_solve_error_session(project, n: int = 4, dx: float = 5.0, amp: float = 0.3):
    """One session of ``n`` sub-frames whose stored WCS carries a constant
    ``dx`` px pointing error (issue 12): frame ``i``'s stored WCS is the
    drifted one, but the stars are DRAWN ``dx`` px away from where that WCS
    predicts them. Returns ``(session_id, frames, injected_mags)``."""
    session = start_session(project, clock=frozen(T0))
    injected: list[float] = []
    frames = []
    for i in range(n):
        m_inj = amp * math.sin(2.0 * math.pi * i / n)
        injected.append(m_inj)
        target_peak = F0 * 10 ** (-0.4 * m_inj)
        sx, sy = drift(i)
        drawn = offset_positions(sx, sy, dx, 0.0)
        frame = store_frame(
            project,
            session,
            KIND_SUB,
            fits_bytes(
                frame_data(drawn, target_peak, seed=200 + i),
                date_obs=date_obs_str(i),
            ),
        )
        save_frame_wcs(
            project,
            frame.id,
            solver="gaia",
            n_stars=12,
            center_ra_deg=CRVAL[0],
            center_dec_deg=CRVAL[1],
            cards=wcs_cards(sx, sy),
        )
        frames.append(frame)
    return session, frames, injected


def test_measure_project_centroid_refines_reprojected_picks_on_every_frame(project):
    n, dx = 4, 5.0
    _session, _frames, injected = make_solve_error_session(project, n=n, dx=dx)
    save_star_picks(project, make_picks())

    report = measure_project(project, load_star_picks(project))
    assert isinstance(report, MeasureReport)
    assert len(report.measurements) == n and report.skipped == ()
    assert report.wcs_frames == n
    assert report.refined_frames == n  # issue 12: every measured frame refined
    assert report.pixel_frames == 0
    for i, fm in enumerate(report.frame_results):
        drawn = offset_positions(*drift(i), dx, 0.0)
        assert fm.wcs_reprojected == 4 and fm.wcs_refined == 4
        assert (fm.target.x, fm.target.y) == pytest.approx(drawn["target"], abs=0.5)
        assert fm.target.pick_offset == pytest.approx((dx, 0.0), abs=0.5)
        assert fm.mag == pytest.approx(injected[i], abs=0.02)
    lc = lightcurve(project)
    assert len(lc) == n
    for meas, m_inj in zip(lc, injected):
        assert meas.mag == pytest.approx(m_inj, abs=0.02)


def test_run_session_reports_and_persists_refined_frames(project):
    """The run.py pass-through of issue 12: ``run_session``'s report aggregates
    the centroid-refined frames (``refined_frames``) and persists them in the
    project.json ``last_run`` record (``wcs_refined_frames``)."""
    n, dx = 4, 5.0
    session, _frames, injected = make_solve_error_session(project, n=n, dx=dx)
    save_star_picks(project, make_picks())

    ctrl = RunController(project=project)
    report = ctrl.run_session(session)
    assert isinstance(report, RunReport)
    assert len(report.measurements) == n and report.skipped == ()
    assert report.wcs_frames == n and report.pixel_frames == 0
    assert report.refined_frames == n  # issue 12
    assert all(fm.wcs_refined == 4 for fm in report.frame_results)
    assert report.frame_results[0].target.pick_offset == pytest.approx((dx, 0.0), abs=0.5)
    for meas, m_inj in zip(report.measurements, injected):
        assert meas.mag == pytest.approx(m_inj, abs=0.02)
    # ... persisted into project.json's last_run record
    last = project.config["photometry"]["last_run"]
    assert last["wcs_frames"] == n and last["pixel_frames"] == 0
    assert last["wcs_refined_frames"] == n  # issue 12


# --- package boundary (subprocess + source scan) -------------------------------------


def test_wcs_reprojection_modules_import_no_gi_and_stay_pure():
    code = (
        "import sys;"
        "import seestar_controller.photometry;"
        "import seestar_controller.photometry.measure;"
        "import seestar_controller.photometry.run;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported eagerly';"
        "assert 'seestar_controller.enhance' not in sys.modules, 'enhance imported eagerly';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_measure_and_run_source_scan_stays_pure():
    for name in ("measure.py", "run.py", "picking.py"):
        source = (PHOTOMETRY_PACKAGE_DIR / name).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), name
        assert not re.search(r"^\s*(from|import)\s+.*\b(ui|link|enhance)\b", source, re.MULTILINE), name


def test_public_api_exports_the_new_helpers():
    for name in ("resolve_pick_pixels", "try_sky_to_pixel", "refine_pick_on_frame"):
        assert name in photometry.__all__, name
        assert hasattr(photometry, name), name
    assert photometry.resolve_pick_pixels is resolve_pick_pixels
    assert photometry.try_sky_to_pixel is try_sky_to_pixel
    assert photometry.refine_pick_on_frame is refine_pick_on_frame
