"""Pure-layer tests for photometry issue 15 — the extended ``mag_err`` model
in :mod:`seestar_controller.photometry.measure`.

The photon/read-noise-only propagation (``MAG_ERR_COEFF * flux_err / flux``)
underestimates the real scatter by ~50x on small-aperture runs (live run
2026-11-22: mag_err median 0.006 vs 0.30 mag robust scatter at r=1.5 px). The
model now folds in, in quadrature on the flux:

- the **positional/centering term** — the centroid uncertainty of a refined
  reprojected pick plus a residual fraction of its ``pick_offset``, the WCS
  match rms in px (:func:`_wcs_rms_px`) when the frame WCS is a
  :class:`~seestar_controller.store.wcs.FrameWcs` carrying ``rms_arcsec``
  (it bounds every position in the frame, including stale fixed picks), and a
  default few-px pointing error for unrefined reprojected picks — converted
  through the aperture-flux curvature :func:`_aperture_flux_curvature`, which
  blows up at small aperture radii;
- the **small-aperture systematics floor** :func:`aperture_systematics_frac` —
  the uncertain fraction of star light outside the aperture.

Covered: the pure model helpers (curvature, positional fraction, systematics
fraction, PSF-sigma estimate, pixel scale, WCS rms), :func:`measure_star`'s new
``positional_err_px`` / ``wcs_err_px`` / ``pick_offset`` terms, the
:func:`measure_frame_data` wiring (WCS rms for every pick, refined vs
unrefined reprojected picks), the store path (:func:`measure_project` with a
saved per-frame WCS rms), and the SANITY TEST: synthetic frames with known
noise AND known per-star pointing jitter — the median ``mag_err`` must be
within a factor ~2 of the empirical scatter of the differential mags across
frames (with the photon-only model it is ~100x too small).

Headless: no ``gi`` anywhere (the sandbox has no GTK, see
docs/wiki/assumptions.md).
"""

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from seestar_controller import photometry
from seestar_controller.photometry import (
    ApertureParams,
    StarPickSpec,
    aperture_systematics_frac,
    differential_mag_err,
    flux_err_to_mag_err,
    measure_frame_data,
    measure_project,
    measure_star,
    positional_flux_frac,
)
from seestar_controller.photometry.measure import (
    APERTURE_SYS_COEFF,
    CENTROID_SIGMA_COEFF,
    DEFAULT_REPROJECT_ERR_PX,
    MAG_ERR_COEFF,
    MIN_PSF_SIGMA_PX,
    PICK_OFFSET_RESIDUAL_FRAC,
    POSITION_ERR_COEFF,
    POSITION_FRAC_MAX,
    REPROJECT_SEARCH_RADIUS_PX,
    FrameSkippedError,
    MeasurementError,
    _aperture_flux_curvature,
    _estimate_psf_sigma,
    _pixel_scale_arcsec,
    _wcs_rms_px,
)
from seestar_controller.store import (
    KIND_SUB,
    create_project,
    save_frame_wcs,
    start_session,
    store_frame,
)
from seestar_controller.store.wcs import FrameWcs

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)

# --- synthetic field ---------------------------------------------------------------
#
# 96x96 frames: flat background + Gaussian stars (2*sigma^2 = 8, sigma = 2 px)
# + Gaussian noise. Star positions are (x, y) = (column, row).

SIZE = 96
BACKGROUND = 100.0
NOISE = 5.0
F0 = 20000.0  # comp peak flux
SIGMA2 = 8.0
REF_POS = {
    "target": (60.0, 48.0),
    "comp_a": (24.0, 24.0),
    "comp_b": (30.0, 70.0),
    "comp_c": (74.0, 30.0),
}

#: The live-run aperture: a SMALL radius (r=1.5 px) with a far-out annulus.
SMALL_APERTURE = ApertureParams(radius_px=1.5, sky_in_px=12.0, sky_out_px=16.0)
#: A normal large aperture for contrast.
LARGE_APERTURE = ApertureParams(radius_px=4.0, sky_in_px=8.0, sky_out_px=12.0)

#: The pixel scale of the synthetic WCS: CDELT 0.001 deg/px = 3.6 arcsec/px.
PIXEL_SCALE_ARCSEC = 3.6


def wcs_cards() -> dict:
    """A plain-dict FITS header with a simple CAR WCS (CDELT +/-0.001 deg/px)."""
    from astropy.wcs import WCS

    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---CAR", "DEC--CAR"]
    w.wcs.crval = [83.0, 22.0]
    w.wcs.crpix = [49.0, 49.0]
    w.wcs.cdelt = [-0.001, 0.001]
    return dict(w.to_header())


CARDS = wcs_cards()


def frame_wcs(rms_arcsec: float | None) -> FrameWcs:
    """A :class:`FrameWcs` record on the synthetic CARDS with the given rms."""
    return FrameWcs(
        frame_id=1,
        session_id="s",
        solver="gaia",
        solved_at="2026-11-20T22:00:00",
        rms_arcsec=rms_arcsec,
        n_stars=12,
        center_ra_deg=83.0,
        center_dec_deg=22.0,
        cards=CARDS,
    )


def frame_data(
    positions: dict[str, tuple[float, float]], target_peak: float, seed: int
) -> np.ndarray:
    """A synthetic frame: the target at ``positions["target"]`` with
    ``target_peak``, the 3 comps at their positions with peak ``F0``, plus
    Gaussian noise, float32."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    data = np.full((SIZE, SIZE), BACKGROUND, dtype=np.float64)
    for key, (x, y) in positions.items():
        peak = target_peak if key == "target" else F0
        data = data + peak * np.exp(-(((yy - y) ** 2 + (xx - x) ** 2) / SIGMA2))
    data = data + rng.normal(0.0, NOISE, size=(SIZE, SIZE))
    return np.ascontiguousarray(data, dtype=np.float32)


def fixed_picks() -> list[StarPickSpec]:
    """One target + 3 comps as FIXED-pixel picks (no sky coordinate)."""
    return [
        StarPickSpec(role="target", x=REF_POS["target"][0], y=REF_POS["target"][1], label="T")
    ] + [
        StarPickSpec(role="comp", x=REF_POS[k][0], y=REF_POS[k][1], label=k)
        for k in ("comp_a", "comp_b", "comp_c")
    ]


def date_obs_str(i: int, seconds: int = 10) -> str:
    instant = T0 + timedelta(seconds=seconds * i)
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "")


def fits_bytes(data: np.ndarray, *, date_obs: str | None = "2026-11-20T22:00:00.000") -> bytes:
    import io

    from astropy.io import fits as pyfits

    hdu = pyfits.PrimaryHDU(data=np.asarray(data, dtype=np.float32))
    if date_obs is not None:
        hdu.header["DATE-OBS"] = date_obs
    hdu.header["EXPTIME"] = 10.0
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
    p = create_project("VW Cep night", root=root)
    yield p
    p.close()


# --- the pure error-model helpers ------------------------------------------------------


def test_aperture_flux_curvature_values_and_limits():
    # c2(r, s) = r^2 e^{-r^2/2s^2} / (4 s^4 (1 - e^{-r^2/2s^2}))
    assert _aperture_flux_curvature(1.5, 2.0) == pytest.approx(0.10824, rel=1e-4)
    assert _aperture_flux_curvature(4.0, 2.0) == pytest.approx(0.03913, rel=1e-4)
    # small-aperture limit r -> 0: c2 -> 1 / (2 sigma^2)
    assert _aperture_flux_curvature(1e-6, 2.0) == pytest.approx(1.0 / 8.0, rel=1e-3)
    # large aperture: the star is encircled, the curvature vanishes
    assert _aperture_flux_curvature(20.0, 2.0) < 1e-6
    # for a fixed sigma the curvature decreases with the radius (past the
    # r -> 0 limit): a small aperture sits in the steep wing of the star
    assert _aperture_flux_curvature(1.5, 2.0) > _aperture_flux_curvature(4.0, 2.0)
    # a broader PSF is less sensitive at a given small radius
    assert _aperture_flux_curvature(1.5, 4.0) < _aperture_flux_curvature(1.5, 2.0)
    # sigma_psf is floored at MIN_PSF_SIGMA_PX, not rejected
    assert _aperture_flux_curvature(1.5, 0.0) == _aperture_flux_curvature(1.5, MIN_PSF_SIGMA_PX)
    for bad_r, bad_s in ((0.0, 2.0), (-1.0, 2.0), (1.5, float("nan")), ("x", 2.0)):
        with pytest.raises(MeasurementError):
            _aperture_flux_curvature(bad_r, bad_s)  # ty: ignore[invalid-argument-type]


def test_positional_flux_frac_model_and_saturation():
    # zero positional error -> no term
    assert positional_flux_frac(1.5, 2.0, 0.0) == 0.0
    # the model: sqrt(3) * c2 * err^2
    c2 = _aperture_flux_curvature(1.5, 2.0)
    assert positional_flux_frac(1.5, 2.0, 1.5) == pytest.approx(
        POSITION_ERR_COEFF * c2 * 1.5**2
    )
    assert POSITION_ERR_COEFF == pytest.approx(math.sqrt(3.0))
    # monotone in the positional error, saturates at the cap
    assert positional_flux_frac(1.5, 2.0, 0.5) < positional_flux_frac(1.5, 2.0, 1.5)
    assert positional_flux_frac(1.5, 2.0, 100.0) == POSITION_FRAC_MAX
    # a large aperture is much less sensitive to the same positional error
    assert positional_flux_frac(4.0, 2.0, 1.5) < 0.5 * positional_flux_frac(1.5, 2.0, 1.5)
    with pytest.raises(MeasurementError):
        positional_flux_frac(1.5, 2.0, -1.0)
    with pytest.raises(MeasurementError):
        positional_flux_frac(1.5, 2.0, float("nan"))


def test_aperture_systematics_frac_small_aperture_floor():
    # the fraction of star light OUTSIDE the aperture, times the coeff
    assert aperture_systematics_frac(1.5, 2.0) == pytest.approx(
        APERTURE_SYS_COEFF * math.exp(-(1.5**2) / 8.0)
    )
    # r=1.5 px vs sigma=2 px: ~75% of the light is outside -> a big floor
    assert aperture_systematics_frac(1.5, 2.0) == pytest.approx(0.0151, rel=1e-3)
    # the floor grows as the aperture shrinks (more light outside)
    assert aperture_systematics_frac(2.0, 2.0) == pytest.approx(
        APERTURE_SYS_COEFF * math.exp(-0.5), rel=1e-9
    )
    assert aperture_systematics_frac(1.5, 2.0) > aperture_systematics_frac(2.0, 2.0) > 0.0
    # ... but only for a SMALL aperture: when the aperture misses less than
    # half the light (r >= ~1.18 sigma, e.g. the default r=4 px vs sigma=2 px)
    # the floor is exactly 0 — edge systematics are negligible next to the
    # photon noise there
    assert aperture_systematics_frac(4.0, 2.0) == 0.0
    assert aperture_systematics_frac(3.0, 2.0) == 0.0  # outside frac e^{-9/8} = 0.32 < 0.5
    assert aperture_systematics_frac(2.3, 2.0) > 0.0  # outside frac ~0.52 > 0.5
    # a broader PSF makes the same radius "small": r=4 vs sigma=4 misses 61%
    assert aperture_systematics_frac(4.0, 4.0) == pytest.approx(
        APERTURE_SYS_COEFF * math.exp(-0.5), rel=1e-9
    )
    with pytest.raises(MeasurementError):
        aperture_systematics_frac(0.0, 2.0)


def test_estimate_psf_sigma_recovers_the_synthetic_psf():
    data = frame_data(dict(REF_POS), F0, seed=3).astype(np.float64)
    for key, (x, y) in REF_POS.items():
        sigma = _estimate_psf_sigma(data, x, y, SMALL_APERTURE.radius_px)
        assert sigma == pytest.approx(2.0, rel=0.15), (key, sigma)
    # a flat noise-only window yields the floor, never zero/negative
    flat = np.full((32, 32), BACKGROUND) + np.random.default_rng(1).normal(0, NOISE, (32, 32))
    assert _estimate_psf_sigma(flat, 16.0, 16.0, 4.0) >= MIN_PSF_SIGMA_PX
    # a pick at the image corner works (window clipped)
    assert _estimate_psf_sigma(data, 0.0, 0.0, 4.0) >= MIN_PSF_SIGMA_PX


def test_pixel_scale_arcsec_from_cards():
    assert _pixel_scale_arcsec(CARDS) == pytest.approx(PIXEL_SCALE_ARCSEC, rel=1e-9)
    # a CD matrix: scale = sqrt(|det CD|)
    cd_cards = {
        "CTYPE1": "RA---CAR",
        "CTYPE2": "DEC--CAR",
        "CD1_1": -0.001,
        "CD1_2": 0.0002,
        "CD2_1": -0.0002,
        "CD2_2": 0.001,
    }
    assert _pixel_scale_arcsec(cd_cards) == pytest.approx(
        math.sqrt(abs(-0.001 * 0.001 + 0.0002 * 0.0002)) * 3600.0, rel=1e-9
    )
    # PC * CDELT
    pc_cards = {"CDELT1": -0.002, "CDELT2": 0.002, "PC1_1": 0.5, "PC2_2": 2.0}
    assert _pixel_scale_arcsec(pc_cards) == pytest.approx(0.002 * 3600.0, rel=1e-9)
    # no celestial scale -> None (a raw Seestar sub)
    assert _pixel_scale_arcsec({"DATE-OBS": "2026-11-20T22:00:00"}) is None
    assert _pixel_scale_arcsec({}) is None


def test_wcs_rms_px_only_from_frame_wcs_records():
    assert _wcs_rms_px(None) is None
    assert _wcs_rms_px(CARDS) is None  # plain cards carry no rms
    assert _wcs_rms_px(frame_wcs(None)) is None  # no rms on the record
    assert _wcs_rms_px(frame_wcs(3.6)) == pytest.approx(1.0)  # 3.6 arcsec / 3.6 arcsec/px
    assert _wcs_rms_px(frame_wcs(7.2)) == pytest.approx(2.0)
    # a record with an rms but no derivable pixel scale -> None
    no_scale = FrameWcs(
        frame_id=1, session_id="s", solver="gaia", solved_at="t", rms_arcsec=1.0,
        n_stars=12, center_ra_deg=83.0, center_dec_deg=22.0, cards={"DATE-OBS": "x"},
    )
    assert _wcs_rms_px(no_scale) is None


# --- measure_star: the new mag_err terms -------------------------------------------------


def test_measure_star_positional_wcs_and_pick_offset_terms():
    data = frame_data(dict(REF_POS), F0, seed=11)
    base = measure_star(data, *REF_POS["target"], SMALL_APERTURE, role="target")
    photon_only = flux_err_to_mag_err(base.flux, base.flux_err)
    # baseline: no positional error -> only the photon term + the small
    # aperture systematics floor
    sigma_psf = _estimate_psf_sigma(np.asarray(data, dtype=np.float64), *REF_POS["target"], 1.5)
    frac_sys = aperture_systematics_frac(1.5, sigma_psf)
    assert base.mag_err == pytest.approx(
        flux_err_to_mag_err(base.flux, math.sqrt(base.flux_err**2 + (frac_sys * base.flux) ** 2))
    )
    assert base.mag_err > photon_only  # the systematics floor is in

    # an explicit positional error (e.g. the WCS pointing error) adds the
    # positional term through the aperture-flux curvature
    with_pos = measure_star(
        data, *REF_POS["target"], SMALL_APERTURE, role="target", positional_err_px=1.5
    )
    frac_pos = positional_flux_frac(1.5, sigma_psf, 1.5)
    assert with_pos.mag_err == pytest.approx(
        flux_err_to_mag_err(
            with_pos.flux,
            math.sqrt(
                with_pos.flux_err**2
                + (frac_pos * with_pos.flux) ** 2
                + (frac_sys * with_pos.flux) ** 2
            ),
        )
    )
    assert with_pos.mag_err > 5.0 * base.mag_err

    # the WCS rms term (px) enters the same way
    with_wcs = measure_star(
        data, *REF_POS["target"], SMALL_APERTURE, role="target", wcs_err_px=1.5
    )
    assert with_wcs.mag_err == pytest.approx(with_pos.mag_err, rel=1e-9)

    # a refinement pick_offset adds the centroid uncertainty plus a residual
    # fraction of the offset (the offset itself was corrected by the refinement)
    with_off = measure_star(
        data, *REF_POS["target"], SMALL_APERTURE, role="target", pick_offset=(3.0, 4.0)
    )
    sigma_c = CENTROID_SIGMA_COEFF * sigma_psf * with_off.flux_err / with_off.flux
    sigma_pos = math.hypot(sigma_c, PICK_OFFSET_RESIDUAL_FRAC * 5.0)
    frac_off = positional_flux_frac(1.5, sigma_psf, sigma_pos)
    assert with_off.mag_err == pytest.approx(
        flux_err_to_mag_err(
            with_off.flux,
            math.sqrt(
                with_off.flux_err**2
                + (frac_off * with_off.flux) ** 2
                + (frac_sys * with_off.flux) ** 2
            ),
        )
    )
    assert with_off.mag_err > base.mag_err  # the pick_offset term is in
    assert with_off.pick_offset == (3.0, 4.0)

    # all three terms combine in quadrature
    combined = measure_star(
        data,
        *REF_POS["target"],
        SMALL_APERTURE,
        role="target",
        pick_offset=(3.0, 4.0),
        wcs_err_px=0.5,
    )
    sigma_pos_all = math.hypot(sigma_pos, 0.5)
    frac_all = positional_flux_frac(1.5, sigma_psf, sigma_pos_all)
    assert combined.mag_err == pytest.approx(
        flux_err_to_mag_err(
            combined.flux,
            math.sqrt(
                combined.flux_err**2
                + (frac_all * combined.flux) ** 2
                + (frac_sys * combined.flux) ** 2
            ),
        )
    )
    assert combined.mag_err > with_off.mag_err

    # bad new arguments raise MeasurementError (not a crash)
    for bad in (-1.0, float("nan"), float("inf"), "x"):
        with pytest.raises(MeasurementError):
            measure_star(
                data, *REF_POS["target"], SMALL_APERTURE, positional_err_px=bad  # ty: ignore[invalid-argument-type]
            )
        with pytest.raises(MeasurementError):
            measure_star(
                data, *REF_POS["target"], SMALL_APERTURE, wcs_err_px=bad  # ty: ignore[invalid-argument-type]
            )


def test_measure_star_small_aperture_systematics_grow_at_small_radius():
    data = frame_data(dict(REF_POS), F0, seed=12)
    small = measure_star(data, *REF_POS["target"], SMALL_APERTURE, role="target")
    large = measure_star(data, *REF_POS["target"], LARGE_APERTURE, role="target")
    # same star, same frame: the small aperture's mag_err is dominated by the
    # edge systematics floor, the large one (r=4 >= ~1.18 sigma) is photon-only
    assert small.mag_err > 3.0 * large.mag_err
    sigma_psf_large = _estimate_psf_sigma(
        np.asarray(data, dtype=np.float64), *REF_POS["target"], 4.0
    )
    assert aperture_systematics_frac(4.0, sigma_psf_large) == 0.0
    assert large.mag_err == pytest.approx(flux_err_to_mag_err(large.flux, large.flux_err))


# --- measure_frame_data: the WCS rms / reprojection wiring --------------------------------


def test_measure_frame_data_wcs_rms_enters_every_picks_mag_err():
    data = frame_data(dict(REF_POS), F0, seed=21)
    header = {"DATE-OBS": date_obs_str(0)}
    picks = fixed_picks()
    no_wcs = measure_frame_data(data, header, picks, SMALL_APERTURE, wcs=None)
    no_rms = measure_frame_data(data, header, picks, SMALL_APERTURE, wcs=frame_wcs(None))
    with_rms = measure_frame_data(
        data, header, picks, SMALL_APERTURE, wcs=frame_wcs(7.2)  # 2 px
    )
    # no WCS == a WCS record without rms (the term is simply absent)
    assert no_rms.mag_err == pytest.approx(no_wcs.mag_err, rel=1e-12)
    # a WCS rms of 2 px inflates every star's mag_err through the positional term
    assert with_rms.mag_err > 5.0 * no_wcs.mag_err
    assert with_rms.target.mag_err > 5.0 * no_wcs.target.mag_err
    assert all(c.mag_err > 5.0 * n.mag_err for c, n in zip(with_rms.comps, no_wcs.comps))
    # and the differential error propagates it (quadrature with the comps)
    assert with_rms.mag_err == pytest.approx(
        differential_mag_err(with_rms.target.mag_err, [c.mag_err for c in with_rms.comps])
    )


def test_measure_frame_data_refined_reprojected_pick_gets_centroid_term():
    """A reprojected pick with a ~5 px solve error: the refinement corrects
    the offset (pick_offset recorded) and mag_err carries the centroid +
    residual-offset positional term, far above the photon-only value."""
    from seestar_controller.photometry import pixel_to_sky, sky_to_pixel

    # sky coordinates of the reference positions
    coords = {key: pixel_to_sky(CARDS, x, y) for key, (x, y) in REF_POS.items()}
    picks = [
        StarPickSpec(role="target", x=0.0, y=0.0, coord=coords["target"], label="T")
    ] + [
        StarPickSpec(role="comp", x=0.0, y=0.0, coord=coords[k], label=k)
        for k in ("comp_a", "comp_b", "comp_c")
    ]
    # the WCS predicts each star 5 px off from where it is drawn (solve error)
    predicted = {k: sky_to_pixel(CARDS, (c.ra_deg, c.dec_deg)) for k, c in coords.items()}
    drawn = {k: (px + 5.0, py + 0.0) for k, (px, py) in predicted.items()}
    data = frame_data(drawn, F0, seed=31)
    fm = measure_frame_data(
        data, {"DATE-OBS": date_obs_str(0)}, picks, SMALL_APERTURE, wcs=CARDS
    )
    assert fm.wcs_reprojected == 4 and fm.wcs_refined == 4
    off = fm.target.pick_offset
    assert off is not None and math.hypot(*off) == pytest.approx(5.0, abs=0.5)
    photon_only = flux_err_to_mag_err(fm.target.flux, fm.target.flux_err)
    # the centroid + residual-offset term dominates over the photon term
    assert fm.target.mag_err > 5.0 * photon_only
    assert fm.mag_err > 5.0 * photon_only


def test_measure_frame_data_unrefined_reprojected_pick_gets_wcs_rms_term():
    """A faint reprojected pick (below the refinement significance threshold)
    with a ~6 px solve error is NOT refined (pick_offset None, wcs_refined 0)
    but still measured at the predicted pixel — its mag_err carries the WCS
    rms (here 6 px via rms_arcsec) positional term, saturating the model."""
    from seestar_controller.photometry import pixel_to_sky, sky_to_pixel

    coords = {key: pixel_to_sky(CARDS, x, y) for key, (x, y) in REF_POS.items()}
    picks = [
        StarPickSpec(role="target", x=0.0, y=0.0, coord=coords["target"], label="T")
    ] + [
        StarPickSpec(role="comp", x=0.0, y=0.0, coord=coords[k], label=k)
        for k in ("comp_a", "comp_b", "comp_c")
    ]
    predicted = {k: sky_to_pixel(CARDS, (c.ra_deg, c.dec_deg)) for k, c in coords.items()}
    # comps exactly at their predicted pixels (bright); the target faint
    # (peak 25: the pixel value ~125 stays below background + 5 sigma ~ 150)
    # and drawn 5 px off -> no significant local maximum -> NOT refined, but
    # its wing still falls into the r=4 aperture at the predicted pixel
    drawn = dict(predicted)
    drawn["target"] = (predicted["target"][0] + 5.0, predicted["target"][1])
    data = frame_data(drawn, 25.0, seed=41)
    fm = measure_frame_data(
        data,
        {"DATE-OBS": date_obs_str(0)},
        picks,
        LARGE_APERTURE,
        wcs=frame_wcs(6.0 * PIXEL_SCALE_ARCSEC),  # rms = 6 px
    )
    assert fm.wcs_reprojected == 4
    assert fm.wcs_refined == 3  # only the bright comps were refined
    assert fm.target.pick_offset is None
    # the unrefined target carries the full WCS-rms positional term (saturated)
    assert fm.target.mag_err > 0.5
    # without any rms the default few-px pointing error applies instead
    fm2 = measure_frame_data(
        data, {"DATE-OBS": date_obs_str(0)}, picks, LARGE_APERTURE, wcs=CARDS
    )
    assert fm2.target.mag_err > 0.5
    # both saturate the positional cap here, but the 6 px rms is never smaller
    # than the 2.5 px default (the rms-vs-default ordering is covered exactly
    # by the fixed-picks test above)
    assert fm.target.mag_err >= fm2.target.mag_err
    assert DEFAULT_REPROJECT_ERR_PX == pytest.approx(0.5 * REPROJECT_SEARCH_RADIUS_PX)


# --- the store path: measure_project picks up the saved per-frame WCS rms -----------------


def test_measure_project_uses_saved_wcs_rms_in_mag_err(project):
    session = start_session(project, clock=frozen(T0))
    for i in range(4):
        frame = store_frame(
            project,
            session,
            KIND_SUB,
            fits_bytes(frame_data(dict(REF_POS), F0, seed=50 + i), date_obs=date_obs_str(i)),
        )
        save_frame_wcs(
            project,
            frame.id,
            solver="gaia",
            n_stars=12,
            center_ra_deg=83.0,
            center_dec_deg=22.0,
            cards=CARDS,
            rms_arcsec=7.2,  # 2 px
        )
    report = measure_project(project, fixed_picks(), SMALL_APERTURE)
    assert len(report.measurements) == 4 and report.skipped == ()
    for fm in report.frame_results:
        photon_only = flux_err_to_mag_err(fm.target.flux, fm.target.flux_err)
        # the 2 px WCS rms positional term dominates the photon term
        assert fm.target.mag_err > 5.0 * photon_only
        assert fm.mag_err > 5.0 * photon_only


# --- SANITY TEST: synthetic frames with known noise + known pointing jitter --------------
#
# The live-run configuration: a SMALL aperture (r=1.5 px), fixed pixel picks,
# and a per-frame pointing jitter of sigma_j px per star (the astrometric
# error the per-frame solved WCS reports as its match rms). The empirical
# scatter of the differential mags across frames must be matched by mag_err
# within a factor ~2 — the photon-only model is ~100x too small here.

JITTER_SIGMA_PX = 1.5
N_FRAMES = 48


def test_mag_err_within_factor_2_of_empirical_scatter_on_synthetic_noise():
    rng = np.random.default_rng(20261122)
    wcs = frame_wcs(JITTER_SIGMA_PX * PIXEL_SCALE_ARCSEC)  # the WCS reports the jitter as its rms
    picks = fixed_picks()
    mags: list[float] = []
    errs: list[float] = []
    photon_only_errs: list[float] = []
    measured = 0
    for i in range(N_FRAMES):
        positions = {
            k: (x + rng.normal(0.0, JITTER_SIGMA_PX), y + rng.normal(0.0, JITTER_SIGMA_PX))
            for k, (x, y) in REF_POS.items()
        }
        data = frame_data(positions, F0, seed=1000 + i)
        try:
            fm = measure_frame_data(
                data, {"DATE-OBS": date_obs_str(i)}, picks, SMALL_APERTURE, wcs=wcs
            )
        except FrameSkippedError:
            continue  # a rare large jitter puts the core outside the tiny aperture
        measured += 1
        mags.append(fm.mag)
        errs.append(fm.mag_err)
        photon_only_errs.append(
            differential_mag_err(
                flux_err_to_mag_err(fm.target.flux, fm.target.flux_err),
                [flux_err_to_mag_err(c.flux, c.flux_err) for c in fm.comps],
            )
        )
    assert measured >= 40  # essentially every frame is measured
    mags_arr = np.asarray(mags)
    errs_arr = np.asarray(errs)
    scatter = float(mags_arr.std(ddof=1))  # the empirical scatter across frames
    med_err = float(np.median(errs_arr))
    # the scenario is meaningful: a real scatter, and the old photon-only
    # model would be far below it
    assert scatter > 0.1
    assert scatter > 10.0 * float(np.median(photon_only_errs))
    # THE SANITY CHECK: mag_err within a factor ~2 of the empirical scatter
    assert 0.5 <= med_err / scatter <= 2.0, (med_err, scatter)
    # every individual frame's mag_err is in the same ballpark (not just the median)
    assert np.all(errs_arr > 0.25 * scatter)
    assert np.all(errs_arr < 4.0 * scatter)


def test_same_frames_without_wcs_rms_have_photon_like_mag_err():
    """Without a WCS rms the model has no positional information: the same
    jittered frames then yield a photon-only mag_err, ~100x below the
    scatter — which is exactly what the WCS-rms term fixes."""
    rng = np.random.default_rng(20261122)
    picks = fixed_picks()
    mags: list[float] = []
    errs: list[float] = []
    for i in range(N_FRAMES):
        positions = {
            k: (x + rng.normal(0.0, JITTER_SIGMA_PX), y + rng.normal(0.0, JITTER_SIGMA_PX))
            for k, (x, y) in REF_POS.items()
        }
        data = frame_data(positions, F0, seed=1000 + i)
        try:
            fm = measure_frame_data(
                data, {"DATE-OBS": date_obs_str(i)}, picks, SMALL_APERTURE, wcs=None
            )
        except FrameSkippedError:
            continue
        mags.append(fm.mag)
        errs.append(fm.mag_err)
    scatter = float(np.asarray(mags).std(ddof=1))
    med_err = float(np.median(errs))
    assert scatter > 10.0 * med_err  # no astrometric information -> underestimated


# --- package exports ----------------------------------------------------------------------


def test_mag_err_model_functions_are_exported_from_package():
    assert photometry.positional_flux_frac is positional_flux_frac
    assert photometry.aperture_systematics_frac is aperture_systematics_frac
    for name in ("positional_flux_frac", "aperture_systematics_frac"):
        assert name in photometry.__all__, name
    # the constants are documented on the measure module
    for const in (
        "CENTROID_SIGMA_COEFF",
        "PICK_OFFSET_RESIDUAL_FRAC",
        "DEFAULT_REPROJECT_ERR_PX",
        "POSITION_ERR_COEFF",
        "POSITION_FRAC_MAX",
        "APERTURE_SYS_COEFF",
        "MIN_PSF_SIGMA_PX",
    ):
        assert isinstance(getattr(photometry.measure, const), float), const
    assert MAG_ERR_COEFF == pytest.approx(2.5 / math.log(10.0))
