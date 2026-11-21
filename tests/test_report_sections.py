"""Pure-layer tests for session-report ticket 02 — the OPTIONAL quality and
movers sections of the report (HTML + PDF).

Covered:

- a synthetic session whose sub-frames are scored via
  :func:`seestar_controller.store.save_frame_quality` -> the **frame-quality**
  section appears in the HTML (``id="frame-quality"``, the caption with the
  scored-frame count / median FWHM / % rejected, the flagged-frames table with
  the flag reasons, and the FWHM-vs-JD sparkline as a 4th embedded PNG) AND
  in the PDF (same heading + caption + flagged table);
- a session WITHOUT scored frames -> ``collect_report_data(...).quality`` is
  None and the section is gracefully OMITTED from both documents (and no
  quality PNG is embedded);
- the **movers** section: ``seestar_controller.moving_objects`` does not exist
  on main, so by default ``data.movers`` is None and the section is omitted;
  with a fake ``moving_objects`` module injected into ``sys.modules`` the
  section appears (HTML + PDF) with the coerced kind/label/position/frames;
  a fake whose ``session_movers`` raises (or returns nothing) -> omitted;
- :func:`~seestar_controller.report.load_movers` on its own (None without the
  package, coercion with a fake, empty -> None, TypeError on a non-string
  session id);
- :class:`~seestar_controller.report.QualitySummary` properties and
  :func:`~seestar_controller.report.quality_trend_png` (valid PNG for points
  and for an empty set);
- NO NETWORK — one test monkeypatches :mod:`socket` to raise while generating
  both documents with quality + mover data present.

Headless: no ``gi`` anywhere; the package boundary lives in
``tests/test_report_boundary.py``.
"""

from __future__ import annotations

import base64
import io
import re
import socket
import sys
import types
import zlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from astropy.io import fits as pyfits

from seestar_controller.report import (
    IMAGE_KEYS,
    OPTIONAL_SECTION_IDS,
    SECTION_MOVERS,
    SECTION_QUALITY,
    QualityPoint,
    QualitySummary,
    collect_report_data,
    generate_pdf,
    generate_report,
    load_movers,
    quality_trend_png,
)
from seestar_controller.store import (
    KIND_STACK,
    KIND_SUB,
    close_session,
    create_project,
    save_frame_quality,
    start_session,
    store_frame,
)

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)
JD0 = 2460000.5
PERIOD = 0.5

#: The FWHM values saved for the scored session's frames (the last one is the
#: flagged outlier).
FWHMS = (2.0, 2.2, 2.4, 2.6, 2.8, 9.5)


def frozen(dt: datetime):
    return lambda: dt


def fits_bytes(date_obs: str, seed: int = 0, shape: tuple[int, int] = (32, 32)) -> bytes:
    """A small valid FITS file with a structured (non-flat) image and the
    given DATE-OBS (drives the frame jd)."""
    rng = np.random.default_rng(seed)
    _y, x = np.mgrid[0 : shape[0], 0 : shape[1]]
    data = (1000.0 + 500.0 * np.sin(x / 5.0) + rng.normal(0.0, 20.0, shape)).astype(np.float32)
    hdu = pyfits.PrimaryHDU(data=data)
    hdu.header["DATE-OBS"] = date_obs
    hdu.header["EXPTIME"] = 10.0
    hdu.header["OBJECT"] = "M 42"
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    return buf.getvalue()


def date_obs_str(offset_seconds: float) -> str:
    instant = T0 + timedelta(seconds=offset_seconds)
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "")


def _embedded_pngs(text: str) -> list[bytes]:
    payloads = re.findall(r"data:image/png;base64,([A-Za-z0-9+/=]+)", text)
    return [base64.b64decode(p) for p in payloads]


def pdf_text(pdf: bytes) -> bytes:
    """Decode the PDF's content streams (ASCII85 + Flate, reportlab's
    default) — enough to assert on the drawn text."""
    out: list[bytes] = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", pdf, re.DOTALL):
        raw = match.group(1).strip()
        decoded: bytes | None = None
        try:
            decoded = zlib.decompress(raw)
        except zlib.error:
            try:
                decoded = zlib.decompress(base64.a85decode(raw, adobe=True))
            except (ValueError, zlib.error):
                decoded = None
        if decoded is not None:
            out.append(decoded)
    return b"\n".join(out)


def assert_valid_pdf(pdf: bytes) -> None:
    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert len(pdf) > 1000
    assert len(re.findall(rb"/Type\s*/Page[^s]", pdf)) >= 1


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    p = create_project("M 42 night", root=root)
    yield p
    p.close()


@pytest.fixture()
def scored_session(project) -> str:
    """One closed session with 6 scored sub-frames (the last one flagged as
    an outlier), one stack, and a persisted period."""
    sid = start_session(project, clock=frozen(T0))
    for i, fwhm in enumerate(FWHMS):
        frame = store_frame(
            project,
            sid,
            KIND_SUB,
            fits_bytes(date_obs_str(60.0 * i), seed=i),
            meta={"exposure": 10.0, "gain": 50.0, "target": "M 42"},
        )
        save_frame_quality(
            project,
            frame.id,
            fwhm=fwhm,
            bg_median=1000.0 + i,
            bg_std=20.0 + 0.1 * i,
            star_count=40 + i,
            flagged=(i == len(FWHMS) - 1),
            flag_reasons=("fwhm",) if i == len(FWHMS) - 1 else (),
        )
    store_frame(project, sid, KIND_STACK, fits_bytes(date_obs_str(600.0), seed=20))
    close_session(project, sid, clock=frozen(T0 + timedelta(hours=1)))
    project.config["period"] = PERIOD
    project.save_config()
    return sid


@pytest.fixture()
def plain_session(project) -> str:
    """One closed session with sub-frames but NO quality records, a stack,
    and a persisted period."""
    sid = start_session(project, clock=frozen(T0))
    for i in range(3):
        store_frame(project, sid, KIND_SUB, fits_bytes(date_obs_str(60.0 * i), seed=i))
    store_frame(project, sid, KIND_STACK, fits_bytes(date_obs_str(300.0), seed=9))
    close_session(project, sid, clock=frozen(T0 + timedelta(minutes=30)))
    project.config["period"] = PERIOD
    project.save_config()
    return sid


def _fake_moving_objects(monkeypatch, session_movers: Any) -> types.ModuleType:
    """Inject a fake ``seestar_controller.moving_objects`` module (the real
    package is a parallel workstream and does not exist on main yet)."""
    import seestar_controller

    fake = types.ModuleType("seestar_controller.moving_objects")
    fake.session_movers = session_movers  # type: ignore[attr-defined]  # ty: ignore[missing-attribute]
    monkeypatch.setitem(sys.modules, "seestar_controller.moving_objects", fake)
    monkeypatch.setattr(seestar_controller, "moving_objects", fake, raising=False)
    return fake


# --- the quality section (HTML + PDF) ----------------------------------------------


def test_quality_section_in_html(project, scored_session):
    data = collect_report_data(project, scored_session)
    assert data.quality is not None
    q = data.quality
    assert q.frame_count == len(FWHMS)
    assert q.flagged_count == 1
    assert q.rejected_percent == pytest.approx(100.0 / len(FWHMS))
    assert q.median_fwhm == pytest.approx(2.5)
    assert q.flagged[0].flag_reasons == ("fwhm",)

    text = generate_report(project, scored_session).read_text(encoding="utf-8")
    assert f'id="{SECTION_QUALITY}"' in text
    assert "Frame quality" in text
    assert "6 scored frame(s)" in text
    assert "median FWHM 2.50 px" in text
    assert "16.7% rejected (1 flagged)" in text
    # the flagged-frames table with the flag reason
    assert "flag reasons" in text
    assert "fwhm" in text
    # the sparkline is the 4th embedded PNG (thumbnail + lightcurve + fold + quality)
    pngs = _embedded_pngs(text)
    assert len(pngs) == 4
    for png in pngs:
        assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_quality_section_in_pdf(project, scored_session):
    generate_report(project, scored_session)
    pdf_path = generate_pdf(project, scored_session)
    raw = pdf_path.read_bytes()
    assert_valid_pdf(raw)
    text = pdf_text(raw)
    assert b"Frame quality" in text
    # NB: reportlab escapes parentheses in PDF text strings ("\("), so the
    # assertions avoid them
    assert b"6 scored frame" in text
    assert b"median FWHM 2.50 px" in text
    assert b"16.7% rejected" in text
    assert b"1 flagged" in text
    assert b"flag reasons" in text
    assert b"fwhm" in text


def test_quality_section_omitted_without_scored_frames(project, plain_session):
    data = collect_report_data(project, plain_session)
    assert data.quality is None

    text = generate_report(project, plain_session).read_text(encoding="utf-8")
    assert f'id="{SECTION_QUALITY}"' not in text
    assert "Frame quality" not in text
    # no quality sparkline: thumbnail + lightcurve + fold only
    assert len(_embedded_pngs(text)) == 3

    raw = generate_pdf(project, plain_session).read_bytes()
    assert_valid_pdf(raw)
    assert b"Frame quality" not in pdf_text(raw)


# --- the movers section (gracefully omitted by default) -----------------------------


def test_movers_section_omitted_when_no_moving_objects_package(project, plain_session):
    # seestar_controller.moving_objects does not exist on main -> no data
    assert "seestar_controller.moving_objects" not in sys.modules
    data = collect_report_data(project, plain_session)
    assert data.movers is None

    text = generate_report(project, plain_session).read_text(encoding="utf-8")
    assert f'id="{SECTION_MOVERS}"' not in text
    assert "Moving objects" not in text
    raw = generate_pdf(project, plain_session).read_bytes()
    assert b"Moving objects" not in pdf_text(raw)


def test_movers_section_present_with_moving_objects(project, plain_session, monkeypatch):
    _fake_moving_objects(
        monkeypatch,
        lambda project, sid: [
            SimpleNamespace(
                kind="asteroid", label="2026 AB", ra_deg=83.6, dec_deg=22.0, n_frames=12
            ),
            SimpleNamespace(type="satellite", name="SAT-1", x=100.5, y=42.0, frame_count=3),
        ],
    )
    data = collect_report_data(project, plain_session)
    assert data.movers is not None
    assert len(data.movers) == 2
    first, second = data.movers
    assert (first.kind, first.label) == ("asteroid", "2026 AB")
    assert "ra 83.60000" in first.position and "dec +22.00000" in first.position
    assert first.frames == "12 frame(s)"
    assert (second.kind, second.label) == ("satellite", "SAT-1")
    assert "x 100.5 px" in second.position
    assert second.frames == "3 frame(s)"

    text = generate_report(project, plain_session).read_text(encoding="utf-8")
    assert f'id="{SECTION_MOVERS}"' in text
    assert "Moving objects" in text
    assert "2 moving object(s) detected" in text
    assert "2026 AB" in text
    assert "asteroid" in text
    assert "ra 83.60000" in text

    raw = generate_pdf(project, plain_session).read_bytes()
    assert_valid_pdf(raw)
    pdf = pdf_text(raw)
    assert b"Moving objects" in pdf
    assert b"2026 AB" in pdf
    assert b"asteroid" in pdf


def test_movers_section_omitted_when_lookup_fails(project, plain_session, monkeypatch):
    def _boom(project, sid):
        raise RuntimeError("mover store unavailable")

    _fake_moving_objects(monkeypatch, _boom)
    data = collect_report_data(project, plain_session)
    assert data.movers is None
    text = generate_report(project, plain_session).read_text(encoding="utf-8")
    assert f'id="{SECTION_MOVERS}"' not in text


def test_movers_section_omitted_when_no_detections(project, plain_session, monkeypatch):
    _fake_moving_objects(monkeypatch, lambda project, sid: [])
    assert collect_report_data(project, plain_session).movers is None
    _fake_moving_objects(monkeypatch, lambda project, sid: None)
    assert collect_report_data(project, plain_session).movers is None


# --- load_movers / QualitySummary / quality_trend_png on their own -------------------


def test_load_movers_without_the_package_returns_none(project, plain_session):
    assert load_movers(project, plain_session) is None


def test_load_movers_type_error_on_non_string(project):
    with pytest.raises(TypeError, match="session_id must be a string"):
        load_movers(project, 123)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_load_movers_coerces_and_handles_empty(project, plain_session, monkeypatch):
    _fake_moving_objects(
        monkeypatch,
        lambda project, sid: [SimpleNamespace(kind="comet", label="C/2026", n_frames=5)],
    )
    movers = load_movers(project, plain_session)
    assert movers is not None and len(movers) == 1
    assert movers[0].kind == "comet" and movers[0].label == "C/2026"
    assert movers[0].frames == "5 frame(s)"
    assert movers[0].position == ""  # no ra/dec, no x/y

    _fake_moving_objects(monkeypatch, lambda project, sid: [])
    assert load_movers(project, plain_session) is None


def test_load_movers_without_session_movers_api(project, plain_session, monkeypatch):
    import seestar_controller

    fake = types.ModuleType("seestar_controller.moving_objects")  # no session_movers
    monkeypatch.setitem(sys.modules, "seestar_controller.moving_objects", fake)
    monkeypatch.setattr(seestar_controller, "moving_objects", fake, raising=False)
    assert load_movers(project, plain_session) is None


def test_quality_summary_properties():
    points = (
        QualityPoint(1, JD0, 2.0, 1000.0, 20.0, 40, False, ()),
        QualityPoint(2, JD0 + 0.01, 4.0, 1001.0, 22.0, 41, True, ("fwhm", "bg")),
        QualityPoint(3, JD0 + 0.02, None, 1002.0, 21.0, 42, False, ()),
    )
    q = QualitySummary(session_id="s", points=points)
    assert q.frame_count == 3
    assert q.flagged_count == 1
    assert q.flagged == (points[1],)
    assert q.rejected_percent == pytest.approx(100.0 / 3.0)
    assert q.median_fwhm == pytest.approx(3.0)  # over the points that have one
    assert q.median_bg_std == pytest.approx(21.0)
    assert q.median_star_count == pytest.approx(41.0)

    empty = QualitySummary(session_id="s", points=())
    assert empty.frame_count == 0
    assert empty.flagged_count == 0
    assert empty.rejected_percent == 0.0
    assert empty.median_fwhm is None
    assert empty.median_bg_std is None
    assert empty.median_star_count is None


def test_quality_trend_png_renders_valid_png():
    points = (
        QualityPoint(1, JD0, 2.0, 1000.0, 20.0, 40, False, ()),
        QualityPoint(2, JD0 + 0.01, 9.5, 1001.0, 22.0, 41, True, ("fwhm",)),
        QualityPoint(3, JD0 + 0.02, None, 1002.0, 21.0, 42, False, ()),  # skipped
    )
    png = quality_trend_png(points, title="Frame quality — test")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    # an empty point set still renders a valid PNG (the "no FWHM" note)
    empty = quality_trend_png(())
    assert empty.startswith(b"\x89PNG\r\n\x1a\n")


# --- constants + no-network -----------------------------------------------------------


def test_optional_section_constants():
    assert SECTION_QUALITY == "frame-quality"
    assert SECTION_MOVERS == "movers"
    assert OPTIONAL_SECTION_IDS == (SECTION_QUALITY, SECTION_MOVERS)
    assert len(IMAGE_KEYS) == 4  # thumbnail, lightcurve, fold, quality-trend


def test_generate_report_and_pdf_never_touch_the_network(
    project, scored_session, monkeypatch
):
    _fake_moving_objects(
        monkeypatch,
        lambda project, sid: [SimpleNamespace(kind="asteroid", label="2026 AB", n_frames=12)],
    )

    def _no_network(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(socket, "getaddrinfo", _no_network)
    html = generate_report(project, scored_session)
    text = html.read_text(encoding="utf-8")
    assert f'id="{SECTION_QUALITY}"' in text
    assert f'id="{SECTION_MOVERS}"' in text
    assert len(_embedded_pngs(text)) == 4
    raw = generate_pdf(project, scored_session).read_bytes()
    assert_valid_pdf(raw)
    pdf = pdf_text(raw)
    assert b"Frame quality" in pdf
    assert b"Moving objects" in pdf
