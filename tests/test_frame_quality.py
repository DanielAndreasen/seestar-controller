"""Frame-quality tickets 01 + 02 — pure metrics core + flagging + session
scoring + the quality-trends computation.

Headless: no ``gi`` anywhere (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary). No
network: everything runs on SYNTHETIC FITS sub-frames stored in a tmp project
via :mod:`seestar_controller.store` (``create_project`` / ``start_session`` /
``store_frame`` — the same pattern as tests/test_photometry_measure.py).

The synthetic frames are 128x128: a flat background + Gaussian stars
(sigma = 1.5 px, FWHM ≈ 3.53 px) + noise. One injected BLURRY frame
(sigma = 4 px, FWHM ≈ 9.4 px) must be flagged by the per-session robust
MAD z-score while the good frames pass; one injected CLOUDY frame
(background 1000, noise 30) must be flagged via the background metrics.

Also covered: ``score_frame`` on arrays / FITS bytes / FITS paths, the flat
frame, RGB reduction, the error cases, the pure flagging math
(``robust_zscores`` / ``flag_outliers`` / ``flag_reasons``), ``score_session``
end-to-end (scoring + flagging + persistence + the ``session_quality`` query
API), skipped frames, stacks never scored, re-scoring upsert, and the package
boundary (subprocess + source scan). Ticket 02's pure trend computation
(airmass series, series extraction, plot transforms, the quality controller)
lives in tests/test_frame_quality_trends.py; the widget layer
(``quality_view``) in tests/test_frame_quality_widgets.py.
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

from seestar_controller import frame_quality
from seestar_controller.frame_quality import (
    DEFAULT_MAD_THRESHOLD,
    FrameMetrics,
    QualityError,
    ScoredFrame,
    SessionScore,
    SkippedFrame,
    flag_outliers,
    flag_reasons,
    robust_zscores,
    score_frame,
    score_session,
    session_quality,
)
from seestar_controller.store import (
    KIND_STACK,
    KIND_SUB,
    SessionNotFoundError,
    create_project,
    frame_quality_for_frame,
    list_frames,
    start_session,
    store_frame,
)
from seestar_controller.store.errors import QualityError as StoreQualityError

#: Directory of the frame_quality package (source-scan audit).
FRAME_QUALITY_PACKAGE_DIR = Path(frame_quality.__file__).resolve().parent

#: The PURE modules of the package (no ``gi`` import — enforced by the source
#: scan + the subprocess test below).
PURE_FRAME_QUALITY_MODULES = ("metrics", "session", "trends")

#: The WIDGET modules of the package (``import gi`` at module top, before the
#: first ``from gi.repository`` line; lazy-exported via PEP 562
#: ``_WIDGET_EXPORTS`` — never imported eagerly by the package ``__init__``).
WIDGET_FRAME_QUALITY_MODULES = ("quality_view",)

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)

# --- synthetic frames -------------------------------------------------------------------

SIZE = 128
BACKGROUND = 100.0
NOISE = 5.0
#: Star positions as (cy, cx) with their peak fluxes.
STARS = (
    (30, 40, 20000.0),
    (16, 16, 20000.0),
    (12, 50, 20000.0),
    (50, 12, 20000.0),
    (90, 90, 15000.0),
    (100, 30, 18000.0),
    (60, 100, 12000.0),
    (20, 100, 9000.0),
    (80, 60, 8000.0),
    (40, 80, 7000.0),
)
#: The sharp-star sigma (px): FWHM = 2.3548 * sigma ≈ 3.53 px.
SHARP_SIGMA = 1.5
#: The injected blurry-frame sigma (px): FWHM ≈ 9.4 px.
BLURRY_SIGMA = 4.0


def make_frame(
    sigma: float = SHARP_SIGMA,
    background: float = BACKGROUND,
    noise: float = NOISE,
    seed: int = 0,
) -> np.ndarray:
    """A synthetic 128x128 frame: flat background + Gaussian stars + noise."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    data = np.full((SIZE, SIZE), background, dtype=np.float64)
    for cy, cx, peak in STARS:
        data = data + peak * np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / (2.0 * sigma**2)))
    data = data + rng.normal(0.0, noise, size=(SIZE, SIZE))
    return np.ascontiguousarray(data, dtype=np.float32)


def good_frames(n: int) -> list[np.ndarray]:
    """``n`` good frames from two alternating seeds.

    Two distinct noise realizations keep the per-session MAD non-zero (so the
    z-scores of the good frames stay at |z| <= 0.6745, far below the 3.0
    threshold) while still exercising a non-degenerate distribution.
    """
    return [make_frame(seed=i % 2) for i in range(n)]


def date_obs_str(i: int, seconds: int = 10) -> str:
    """The DATE-OBS of sub ``i``: T0 + i*seconds, naive ISOT (UTC)."""
    instant = T0 + timedelta(seconds=seconds * i)
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "")


def fits_bytes(data: np.ndarray, *, date_obs: str | None = "2026-11-20T22:00:00.000") -> bytes:
    """A small valid FITS file with the given data (+ DATE-OBS unless None)."""
    hdu = pyfits.PrimaryHDU(data=np.asarray(data, dtype=np.float32))
    if date_obs is not None:
        hdu.header["DATE-OBS"] = date_obs
    hdu.header["EXPTIME"] = 10.0
    hdu.header["OBJECT"] = "M 42"
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


@pytest.fixture()
def root(tmp_path):
    """A fresh project root."""
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    """An open project (closed at teardown)."""
    p = create_project("M 42", root=root)
    yield p
    p.close()


def frozen(dt: datetime):
    return lambda: dt


def store_subs(project, session_id: str, frames: list[np.ndarray]) -> list:
    """Store the synthetic frames as subs of the session, in order."""
    stored = []
    for i, data in enumerate(frames):
        stored.append(
            store_frame(project, session_id, KIND_SUB, fits_bytes(data, date_obs=date_obs_str(i)))
        )
    return stored


# --- score_frame -------------------------------------------------------------------------


def test_score_frame_array_metrics():
    m = score_frame(make_frame(seed=1))
    assert isinstance(m, FrameMetrics)
    assert m.star_count == len(STARS)
    assert m.bg_median == pytest.approx(BACKGROUND, abs=1.0)
    assert m.bg_std == pytest.approx(NOISE, rel=0.2)
    # FWHM of a Gaussian with sigma=1.5 is 2.3548*1.5 ≈ 3.53 px
    assert m.fwhm == pytest.approx(2.3548 * SHARP_SIGMA, abs=0.3)


def test_score_frame_blurry_frame_has_much_larger_fwhm():
    sharp = score_frame(make_frame(seed=1))
    blurry = score_frame(make_frame(sigma=BLURRY_SIGMA, seed=99))
    assert blurry.fwhm is not None and sharp.fwhm is not None
    assert blurry.fwhm > 2.0 * sharp.fwhm


def test_score_frame_cloudy_frame_has_high_background():
    cloudy = score_frame(make_frame(background=1000.0, noise=30.0, seed=7))
    assert cloudy.bg_median == pytest.approx(1000.0, abs=5.0)
    assert cloudy.bg_std == pytest.approx(30.0, rel=0.2)


def test_score_frame_fits_bytes_and_path_agree(tmp_path):
    data = make_frame(seed=3)
    from_bytes = score_frame(fits_bytes(data))
    path = tmp_path / "sub.fits"
    path.write_bytes(fits_bytes(data))
    from_path = score_frame(path)
    from_str = score_frame(str(path))
    for m in (from_bytes, from_path, from_str):
        assert m.star_count == len(STARS)
        assert m.fwhm == pytest.approx(from_bytes.fwhm, abs=1e-9)
        assert m.bg_median == pytest.approx(from_bytes.bg_median, abs=1e-9)


def test_score_frame_flat_frame_has_no_stars():
    m = score_frame(np.full((32, 32), 7.0))
    assert m.star_count == 0
    assert m.fwhm is None
    assert m.bg_median == pytest.approx(7.0)
    assert m.bg_std == pytest.approx(0.0, abs=1e-9)


def test_score_frame_rgb_array_reduced_to_channel_mean():
    data = make_frame(seed=5)
    rgb = np.stack([data, data, data], axis=-1)
    m = score_frame(rgb)
    assert m.star_count == len(STARS)
    assert m.fwhm == pytest.approx(2.3548 * SHARP_SIGMA, abs=0.3)


def test_score_frame_masks_non_finite_pixels():
    data = make_frame(seed=6).astype(np.float64)
    data[0:5, 0:5] = np.nan
    data[60, 60] = np.inf
    m = score_frame(data)
    assert m.star_count == len(STARS)
    assert m.bg_median == pytest.approx(BACKGROUND, abs=1.0)


def test_score_frame_errors():
    with pytest.raises(QualityError, match="not a readable FITS"):
        score_frame(b"this is not a FITS file at all")
    with pytest.raises(QualityError, match="2-D"):
        score_frame(np.zeros(10))
    with pytest.raises(QualityError, match="2-D"):
        score_frame(np.zeros((2, 2, 2, 2)))
    with pytest.raises(QualityError, match="no finite pixel"):
        score_frame(np.full((8, 8), np.nan))
    with pytest.raises(QualityError, match="cannot read frame file"):
        score_frame("/no/such/file.fits")
    # a FITS file without image data
    hdu = pyfits.PrimaryHDU()
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    with pytest.raises(QualityError, match="no image data"):
        score_frame(buf.getvalue())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"fwhm": 0.0},
        {"fwhm": -1.0},
        {"fwhm": float("nan")},
        {"threshold_sigma": 0.0},
        {"sigma_clip": -3.0},
        {"cutout_radius": 0.0},
    ],
)
def test_score_frame_invalid_params_raise(kwargs):
    with pytest.raises(QualityError):
        score_frame(make_frame(seed=1), **kwargs)


# --- pure flagging math -------------------------------------------------------------------


def test_robust_zscores_known_values():
    # median 2, MAD 1 -> z = 0.6744897501960817 * (x - 2)
    zs = robust_zscores([1.0, 2.0, 3.0])
    assert zs == pytest.approx([-0.6744897501960817, 0.0, 0.6744897501960817], abs=1e-12)
    assert robust_zscores([]) == []
    with pytest.raises(QualityError, match="finite"):
        robust_zscores([1.0, float("nan")])


def test_robust_zscores_zero_mad_flags_any_deviation():
    assert robust_zscores([5.0, 5.0, 5.0]) == [0.0, 0.0, 0.0]
    zs = robust_zscores([5.0, 5.0, 7.0])
    assert zs[0] == 0.0 and zs[1] == 0.0
    assert zs[2] == math.inf
    zs = robust_zscores([5.0, 3.0, 5.0])
    assert zs[1] == -math.inf


def _metrics(fwhm, bg_median=100.0, bg_std=5.0, star_count=10):
    return FrameMetrics(fwhm=fwhm, bg_median=bg_median, bg_std=bg_std, star_count=star_count)


def test_flag_outliers_flags_only_the_outlier():
    metrics = [_metrics(3.5) for _ in range(8)]
    metrics[4] = _metrics(9.0)  # blurry
    flagged = flag_outliers(metrics)
    assert flagged == {4}
    reasons = flag_reasons(metrics)
    assert reasons == {4: ("fwhm",)}


def test_flag_outliers_two_sided_and_per_metric():
    metrics = [_metrics(3.5, bg_median=100.0, star_count=10) for _ in range(8)]
    metrics[2] = _metrics(3.5, bg_median=1000.0, star_count=10)  # cloudy: high bg
    metrics[6] = _metrics(3.5, bg_median=100.0, star_count=1)  # lost tracking: low stars
    flagged = flag_outliers(metrics, mad_threshold=3.0)
    assert flagged == {2, 6}
    reasons = flag_reasons(metrics)
    assert reasons[2] == ("bg_median",)
    assert reasons[6] == ("star_count",)


def test_flag_outliers_skips_none_fwhm_but_flags_other_metrics():
    metrics = [_metrics(3.5) for _ in range(5)]
    metrics[1] = _metrics(None, bg_median=100.0)  # no FWHM, but normal bg
    metrics[3] = _metrics(None, bg_median=500.0)  # no FWHM, cloudy
    flagged = flag_outliers(metrics)
    assert flagged == {3}
    assert flag_reasons(metrics)[3] == ("bg_median",)


def test_flag_outliers_threshold_is_configurable_and_validated():
    # a spread of good FWHM values (MAD > 0) plus one mild outlier at index 8
    fwhms = [3.4, 3.45, 3.48, 3.5, 3.52, 3.55, 3.58, 3.6, 4.0]
    metrics = [_metrics(f) for f in fwhms]
    assert flag_outliers(metrics, mad_threshold=100.0) == set()
    assert flag_outliers(metrics, mad_threshold=3.0) == {8}
    assert flag_outliers(metrics, mad_threshold=1.0) == {0, 8}
    with pytest.raises(QualityError, match="mad_threshold"):
        flag_outliers(metrics, mad_threshold=0.0)
    with pytest.raises(QualityError, match="mad_threshold"):
        flag_outliers(metrics, mad_threshold=float("inf"))
    with pytest.raises(QualityError, match="mad_threshold"):
        flag_outliers(metrics, mad_threshold="not-a-number")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(QualityError, match="mad_threshold"):
        flag_outliers(metrics, mad_threshold=True)
    assert DEFAULT_MAD_THRESHOLD == 3.0


def test_flag_outliers_empty_and_constant():
    assert flag_outliers([]) == set()
    assert flag_outliers([_metrics(3.5), _metrics(3.5)]) == set()
    # constant session, one deviating star count -> MAD 0 -> inf z -> flagged
    metrics = [_metrics(3.5, star_count=10) for _ in range(4)]
    metrics[2] = _metrics(3.5, star_count=9)
    assert flag_outliers(metrics) == {2}


# --- score_session end-to-end ---------------------------------------------------------------


def test_score_session_flags_injected_blurry_frame_and_persists(project):
    sid = start_session(project, clock=frozen(T0))
    frames = good_frames(8)
    frames.insert(4, make_frame(sigma=BLURRY_SIGMA, seed=99))  # the blurry sub
    stored = store_subs(project, sid, frames)
    assert len(stored) == 9

    score = score_session(project, sid)
    assert isinstance(score, SessionScore)
    assert score.session_id == sid
    assert score.frame_count == 9 and score.skipped == ()
    assert len(score.frames) == 9
    assert all(isinstance(sf, ScoredFrame) for sf in score.frames)
    # exactly the injected blurry frame is flagged, the good ones pass
    flagged_ids = {sf.frame.id for sf in score.flagged}
    assert flagged_ids == {stored[4].id}
    assert score.flagged_count == 1
    assert score.rejected_percent == pytest.approx(100.0 / 9.0)
    blurry = next(sf for sf in score.frames if sf.frame.id == stored[4].id)
    assert "fwhm" in blurry.reasons
    assert blurry.metrics.fwhm is not None and score.median_fwhm is not None
    assert blurry.metrics.fwhm > 2.0 * score.median_fwhm
    assert score.median_fwhm == pytest.approx(2.3548 * SHARP_SIGMA, abs=0.3)
    # every good frame passes
    for sf in score.frames:
        if sf.frame.id != stored[4].id:
            assert sf.flagged is False and sf.reasons == ()

    # persisted: the query API returns the same table + summary
    q = session_quality(project, sid)
    assert q.frame_count == 9
    assert [f.frame_id for f in q.frames] == [f.id for f in stored]  # jd order
    assert q.flagged_count == 1
    assert q.flagged[0].frame_id == stored[4].id
    assert q.flagged[0].flag_reasons == blurry.reasons
    assert q.rejected_percent == pytest.approx(100.0 / 9.0)
    assert q.median_fwhm == pytest.approx(score.median_fwhm)
    blurry_rec = frame_quality_for_frame(project, stored[4].id)
    good_rec = frame_quality_for_frame(project, stored[0].id)
    assert blurry_rec is not None and blurry_rec.flagged is True
    assert good_rec is not None and good_rec.flagged is False


def test_score_session_flags_injected_cloudy_frame(project):
    sid = start_session(project, clock=frozen(T0))
    frames = good_frames(8)
    frames.append(make_frame(background=1000.0, noise=30.0, seed=7))  # cloudy
    stored = store_subs(project, sid, frames)

    score = score_session(project, sid)
    assert {sf.frame.id for sf in score.flagged} == {stored[8].id}
    cloudy = next(sf for sf in score.frames if sf.frame.id == stored[8].id)
    assert "bg_median" in cloudy.reasons
    assert cloudy.metrics.bg_median > 500.0
    q = session_quality(project, sid)
    assert q.flagged_count == 1 and q.flagged[0].frame_id == stored[8].id


def test_score_session_all_good_frames_pass(project):
    sid = start_session(project, clock=frozen(T0))
    store_subs(project, sid, good_frames(6))
    score = score_session(project, sid)
    assert score.flagged == () and score.flagged_count == 0
    assert score.rejected_percent == 0.0
    q = session_quality(project, sid)
    assert q.frame_count == 6 and q.flagged_count == 0 and q.rejected_percent == 0.0
    assert q.median_fwhm == pytest.approx(2.3548 * SHARP_SIGMA, abs=0.3)


def test_score_session_unknown_session_raises(project):
    with pytest.raises(SessionNotFoundError, match="no session"):
        score_session(project, "no-such-session")
    with pytest.raises(TypeError, match="session_id"):
        score_session(project, 123)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_score_session_empty_session_scores_nothing(project):
    sid = start_session(project, clock=frozen(T0))
    score = score_session(project, sid)
    assert score.frames == () and score.skipped == ()
    assert score.frame_count == 0 and score.rejected_percent == 0.0
    assert score.median_fwhm is None
    q = session_quality(project, sid)
    assert q.frames == () and q.rejected_percent == 0.0


def test_score_session_persist_false_writes_nothing(project):
    sid = start_session(project, clock=frozen(T0))
    stored = store_subs(project, sid, [make_frame(seed=s) for s in range(3)])
    score = score_session(project, sid, persist=False)
    assert len(score.frames) == 3
    assert session_quality(project, sid).frames == ()
    assert all(frame_quality_for_frame(project, f.id) is None for f in stored)
    with pytest.raises(TypeError, match="persist"):
        score_session(project, sid, persist="yes")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_score_session_rescoring_upserts(project):
    sid = start_session(project, clock=frozen(T0))
    store_subs(project, sid, [make_frame(seed=s) for s in range(4)])
    score_session(project, sid)
    score_session(project, sid)  # re-score: upsert, no duplicate rows
    q = session_quality(project, sid)
    assert q.frame_count == 4


def test_score_session_skips_unreadable_frame_never_crashes(project):
    sid = start_session(project, clock=frozen(T0))
    stored = store_subs(project, sid, [make_frame(seed=s) for s in range(3)])
    # corrupt one stored file on disk AFTER storing (store_frame validated it)
    bad = list_frames(project, session_id=sid, kind=KIND_SUB)[1]
    bad_path = project.path / bad.path
    bad_path.write_bytes(b"garbage, not a FITS file anymore")
    score = score_session(project, sid)
    assert len(score.frames) == 2
    assert len(score.skipped) == 1
    assert isinstance(score.skipped[0], SkippedFrame)
    assert score.skipped[0].frame_id == bad.id
    assert "not a readable FITS" in score.skipped[0].reason
    assert score.frame_count == 3
    # the two good frames were still persisted
    q = session_quality(project, sid)
    assert q.frame_count == 2
    assert {f.frame_id for f in q.frames} == {stored[0].id, stored[2].id}


def test_score_session_scores_subs_only_never_stacks(project):
    sid = start_session(project, clock=frozen(T0))
    store_subs(project, sid, [make_frame(seed=s) for s in range(3)])
    store_frame(project, sid, KIND_STACK, fits_bytes(make_frame(seed=42), date_obs=date_obs_str(99)))
    score = score_session(project, sid)
    assert score.frame_count == 3  # the stack is not scored
    assert {sf.frame.id for sf in score.frames} == {
        f.id for f in list_frames(project, session_id=sid, kind=KIND_SUB)
    }
    assert session_quality(project, sid).frame_count == 3


def test_score_session_custom_threshold(project):
    sid = start_session(project, clock=frozen(T0))
    frames = good_frames(8)
    frames.insert(4, make_frame(sigma=BLURRY_SIGMA, seed=99))
    store_subs(project, sid, frames)
    # a huge threshold flags nothing
    score = score_session(project, sid, mad_threshold=1e9)
    assert score.flagged == ()
    assert session_quality(project, sid).flagged_count == 0


# --- package boundary (subprocess + source scan) + public API -------------------------------


def test_frame_quality_package_imports_no_gi_no_link_no_ui():
    code = (
        "import sys;"
        "import seestar_controller.frame_quality;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported eagerly';"
        "assert 'seestar_controller.enhance' not in sys.modules, 'enhance imported eagerly';"
        "assert 'seestar_controller.frame_quality.quality_view' not in sys.modules, 'widget module imported eagerly';"
        "assert 'astropy' not in sys.modules, 'astropy imported eagerly';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_frame_quality_package_source_scan_no_gi_and_module_set():
    for path in sorted(FRAME_QUALITY_PACKAGE_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert not re.search(
            r"^\s*(import|from)\s+seestar_controller\.(link|ui|enhance)\b", source, re.MULTILINE
        ), f"{path.name} imports a widget/link package"
        if path.stem in WIDGET_FRAME_QUALITY_MODULES:
            # widget module: `import gi` at module top, BEFORE the first
            # `from gi.repository` line (the gi-at-top convention)
            lines = source.splitlines()
            gi_import = next(
                (i for i, line in enumerate(lines) if re.match(r"^\s*import gi\b", line)), None
            )
            assert gi_import is not None, f"{path.name} must import gi at module top"
            first_repo = next(
                (
                    i
                    for i, line in enumerate(lines)
                    if re.match(r"^\s*from gi\.repository\b", line)
                ),
                None,
            )
            assert first_repo is not None, f"{path.name} must import from gi.repository"
            assert gi_import < first_repo, f"{path.name}: import gi must precede gi.repository"
        else:
            assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), (
                f"{path.name} imports gi"
            )
    modules = {path.stem for path in FRAME_QUALITY_PACKAGE_DIR.glob("*.py")}
    assert modules == {"__init__", *PURE_FRAME_QUALITY_MODULES, *WIDGET_FRAME_QUALITY_MODULES}
    # the widget layer is lazy-exported (PEP 562), never imported eagerly
    assert frame_quality._WIDGET_EXPORTS == {"SessionQualityView": "quality_view"}
    assert "SessionQualityView" in frame_quality.__all__


def test_frame_quality_public_api_is_exported():
    for name in (
        "DEFAULT_FWHM_PX",
        "DEFAULT_MAD_THRESHOLD",
        "DEFAULT_THRESHOLD_SIGMA",
        "FWHM_CUTOUT_RADIUS_PX",
        "MAD_SCALE",
        "MAX_FWHM_STARS",
        "METRIC_FIELDS",
        "SIGMA_CLIP",
        "FrameMetrics",
        "FrameQuality",
        "QualityError",
        "ScoredFrame",
        "SessionQuality",
        "SessionScore",
        "SkippedFrame",
        "flag_outliers",
        "flag_reasons",
        "frame_quality_for_frame",
        "robust_zscores",
        "save_frame_quality",
        "score_frame",
        "score_session",
        "session_quality",
        # ticket 02: the pure trend layer + the lazy widget export
        "DB_CALL_TIMEOUT_S",
        "DEFAULT_OBSERVER",
        "DEFAULT_TREND_WINDOW",
        "MAX_AIRMASS",
        "Observer",
        "QualityController",
        "QualitySnapshot",
        "SessionQualityView",
        "SessionTrends",
        "TrendPoint",
        "TrendWindow",
        "TrendsError",
        "airmass_series",
        "full_trend_window",
        "resolve_target_coords",
        "screen_to_trend",
        "session_trends",
        "trend_to_screen",
    ):
        assert name in frame_quality.__all__, name
        if name in frame_quality._WIDGET_EXPORTS:
            # lazy widget export: listed in __all__ + _WIDGET_EXPORTS, but NOT
            # imported here (that would pull in gi — headless sandbox)
            assert frame_quality._WIDGET_EXPORTS[name] == "quality_view"
        else:
            assert hasattr(frame_quality, name), name
    assert frame_quality.score_frame is score_frame
    assert frame_quality.score_session is score_session
    assert frame_quality.session_quality is session_quality
    assert issubclass(frame_quality.QualityError, ValueError)
    assert issubclass(frame_quality.TrendsError, frame_quality.QualityError)
    # deliberately distinct from the store's QualityError
    assert not issubclass(frame_quality.QualityError, StoreQualityError)
    assert issubclass(StoreQualityError, Exception)
