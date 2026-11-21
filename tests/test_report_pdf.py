"""Pure-layer tests for session-report ticket 03 — the PDF export and the
per-session saved-reports listing (:mod:`seestar_controller.report.pdf` +
:func:`~seestar_controller.report.generate.generate_pdf` /
:func:`~seestar_controller.report.generate.list_session_reports`).

Headless: no ``gi`` anywhere (the sandbox has no GTK, see
docs/wiki/assumptions.md), and NO NETWORK — one test monkeypatches
:mod:`socket` to raise, proving the PDF export is fully offline (reportlab is
a pure-Python pip dependency, no browser, no subprocess). The package
boundary (incl. the new ``pdf`` module) lives in
``tests/test_report_boundary.py``.

Covered: a synthetic temp project with one session (sub-frames +
measurements, a stack, an enhanced frame, a session_log.jsonl, a persisted
period) -> ``generate_pdf`` writes a NON-EMPTY, valid PDF (``%PDF-`` header,
``%%EOF`` trailer, at least one page, embedded image XObjects) NEXT TO the
HTML report; the PDF text (streams decoded) contains the same sections as the
HTML (title, project/session/generated meta, all five section headings, the
log rows, the period, the measurement values); regeneration overwrites the
same path with no ``*.tmp`` left; ``list_session_reports`` lists exactly the
session's saved reports (HTML + PDF, not other sessions'); unknown session /
bad argument types; ``render_pdf`` on its own (placeholders when images are
None, missing image key -> TypeError); the frozen clock lands in the PDF;
the no-network proof.
"""

from __future__ import annotations

import base64
import io
import json
import math
import re
import socket
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits as pyfits

from seestar_controller.report import (
    IMAGE_KEYS,
    PDF_FILE_SUFFIX,
    REPORTS_DIR,
    ReportError,
    collect_report_data,
    generate_pdf,
    generate_report,
    list_session_reports,
    pdf_path,
    render_pdf,
    report_path,
)
from seestar_controller.store import (
    KIND_ENHANCED,
    KIND_STACK,
    KIND_SUB,
    add_measurement,
    close_session,
    create_project,
    start_session,
    store_frame,
)

T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=UTC)
JD0 = 2460000.5
PERIOD = 0.5


def frozen(dt: datetime):
    return lambda: dt


def fits_bytes(date_obs: str, seed: int = 0, shape: tuple[int, int] = (32, 32)) -> bytes:
    """A small valid FITS file with a structured (non-flat) image and the
    given DATE-OBS (drives the frame jd) + EXPTIME/OBJECT headers."""
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


def write_session_log(project, session_id: str, events: list) -> Path:
    """Write a session_log.jsonl (dicts become JSON lines)."""
    from seestar_controller.report import session_log_path

    path = session_log_path(project, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e) for e in events]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    p = create_project("M 42 night", root=root)
    yield p
    p.close()


@pytest.fixture()
def session(project) -> str:
    """One closed session with 8 sub-frames + measurements (a sinusoid of
    period 0.5 d), one stack, one enhanced frame, a session log, and a
    persisted period."""
    sid = start_session(project, clock=frozen(T0))
    for i in range(8):
        frame = store_frame(
            project,
            sid,
            KIND_SUB,
            fits_bytes(date_obs_str(60.0 * i), seed=i),
            meta={"exposure": 10.0, "gain": 50.0, "target": "M 42"},
        )
        add_measurement(
            project,
            frame.id,
            12.0 + 0.2 * math.sin(2.0 * math.pi * (JD0 + 0.01 * i) / PERIOD),
            jd=JD0 + 0.01 * i,
            mag_err=0.02,
        )
    store_frame(project, sid, KIND_STACK, fits_bytes(date_obs_str(600.0), seed=20))
    store_frame(project, sid, KIND_ENHANCED, fits_bytes(date_obs_str(660.0), seed=21))
    close_session(project, sid, clock=frozen(T0 + timedelta(hours=1)))
    write_session_log(
        project,
        sid,
        [
            {
                "ts": T0.timestamp(),
                "kind": "session",
                "detail": f"session {sid} opened",
                "source": "executor",
                "state": "RUNNING",
                "frame_id": None,
            },
            {
                "ts": (T0 + timedelta(minutes=5)).timestamp(),
                "kind": "stack",
                "detail": "stack finished",
                "source": "capture",
                "state": "",
                "frame_id": None,
            },
        ],
    )
    project.config["period"] = PERIOD
    project.save_config()
    return sid


def pdf_text(pdf: bytes) -> bytes:
    """Decode the PDF's content streams (ASCII85 + Flate, reportlab's
    default) and return the concatenated decompressed bytes — enough to
    assert on the drawn text."""
    out: list[bytes] = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", pdf, re.DOTALL):
        decoded = _inflate_stream(match.group(1).strip())
        if decoded is not None:
            out.append(decoded)
    return b"\n".join(out)


def _inflate_stream(raw: bytes) -> bytes | None:
    """Try plain Flate first, then ASCII85+Flate (reportlab's default)."""
    try:
        return zlib.decompress(raw)
    except zlib.error:
        pass
    try:
        return zlib.decompress(base64.a85decode(raw, adobe=True))
    except (ValueError, zlib.error):
        return None


def assert_valid_pdf(pdf: bytes) -> None:
    """A non-empty, structurally valid PDF: header, EOF trailer, >= 1 page."""
    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert len(pdf) > 1000  # a real document, not a stub
    assert len(re.findall(rb"/Type\s*/Page[^s]", pdf)) >= 1


# --- generate_pdf: a non-empty PDF next to the HTML --------------------------------


def test_generate_pdf_writes_a_non_empty_valid_pdf_next_to_the_html(project, session):
    html = generate_report(project, session)
    pdf = generate_pdf(project, session)
    assert pdf == pdf_path(project, session)
    assert pdf == project.path / REPORTS_DIR / f"{session}{PDF_FILE_SUFFIX}"
    assert pdf.parent == html.parent  # next to the HTML
    assert pdf.is_file()
    raw = pdf.read_bytes()
    assert_valid_pdf(raw)
    # the three report images are embedded as image XObjects (plus their
    # soft masks — so at least three)
    assert len(re.findall(rb"/Subtype\s*/Image", raw)) >= 3
    # the PDF carries the same sections/content as the HTML
    text = pdf_text(raw)
    for needle in (
        b"Session report",
        b"M 42 night",
        session.encode(),
        b"Enhanced stack",
        b"Session log",
        b"Lightcurve",
        b"Period fold",
        b"Measurements summary",
        b"opened",  # a log row (TJ chunks may split words, so short needles)
        b"finished",
        f"{PERIOD:.6f}".encode(),  # the current period
        b"12.000000",  # a measurement value in the summary
        b"8 measurement",  # "(s)" is paren-escaped in the PDF text stream
    ):
        assert needle in text, needle


def test_pdf_regeneration_overwrites_the_same_path_and_leaves_no_tmp(project, session):
    generate_report(project, session)
    pdf = generate_pdf(project, session)
    first = pdf.read_bytes()
    # more data arrives, then regenerate
    frame = store_frame(project, session, KIND_SUB, fits_bytes(date_obs_str(1200.0), seed=30))
    add_measurement(project, frame.id, 99.123, jd=JD0 + 0.2, mag_err=0.05)
    second_path = generate_pdf(project, session)
    assert second_path == pdf
    second = pdf.read_bytes()
    assert second != first
    assert b"99.123000" in pdf_text(second)
    assert b"9 measurement" in pdf_text(second)
    # still exactly the two report files, and no temp files left behind
    reports = sorted((project.path / REPORTS_DIR).iterdir())
    assert reports == [report_path(project, session), pdf]
    assert not list((project.path / REPORTS_DIR).glob("*.tmp"))


def test_generate_pdf_uses_the_injected_clock(project, session):
    stamp = datetime(2026, 12, 1, 8, 30, 0, tzinfo=UTC)
    pdf = generate_pdf(project, session, clock=frozen(stamp))
    assert stamp.isoformat(timespec="milliseconds").encode() in pdf_text(pdf.read_bytes())


def test_generate_pdf_unknown_session_and_bad_argument_types(project, session):
    with pytest.raises(ReportError):
        generate_pdf(project, "no-such-session")
    with pytest.raises(TypeError):
        generate_pdf(project, 123)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        generate_pdf(object(), session)
    with pytest.raises(TypeError):
        pdf_path(project, 123)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        pdf_path(object(), session)


def test_generate_pdf_is_fully_offline(project, session, monkeypatch):
    """No network: any socket use raises — the PDF export must still work."""

    def _no_network(*_args, **_kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    pdf = generate_pdf(project, session)
    assert_valid_pdf(pdf.read_bytes())


# --- list_session_reports: the per-session saved reports ----------------------------


def test_list_session_reports_lists_exactly_the_sessions_files(project, session):
    assert list_session_reports(project, session) == ()  # nothing yet
    html = generate_report(project, session)
    assert list_session_reports(project, session) == (html,)
    pdf = generate_pdf(project, session)
    assert list_session_reports(project, session) == (html, pdf)
    # another session's report is NOT listed for this session
    other = start_session(project, clock=frozen(T0 + timedelta(days=1)))
    close_session(project, other, clock=frozen(T0 + timedelta(days=1, hours=1)))
    generate_report(project, other)
    reports = list_session_reports(project, session)
    assert reports == (html, pdf)
    assert all(p.name.startswith(f"{session}.") for p in reports)
    with pytest.raises(TypeError):
        list_session_reports(project, 42)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        list_session_reports(object(), session)


# --- render_pdf on its own -----------------------------------------------------------


def test_render_pdf_with_placeholder_images_is_still_a_valid_pdf(project, session):
    data = collect_report_data(project, session)
    pdf = render_pdf(data, {key: None for key in IMAGE_KEYS})
    assert_valid_pdf(pdf)
    text = pdf_text(pdf)
    assert b"no image available" in text  # placeholders, never a crash
    assert b"Session report" in text


def test_render_pdf_missing_image_key_raises_type_error(project, session):
    data = collect_report_data(project, session)
    with pytest.raises(TypeError):
        render_pdf(data, {"thumbnail": None})  # lightcurve/fold keys missing


def test_render_pdf_escapes_dynamic_text(project, session):
    """XML special chars in dynamic text must not break the PDF markup."""
    data = collect_report_data(project, session)
    object.__setattr__  # noqa: B018 - no-op, keeps linters quiet about dataclass use
    import dataclasses

    escaped = dataclasses.replace(
        data,
        project_name="A <B> & C",
        log=(
            *data.log,
            type(data.log[0])(
                ts=T0.timestamp(),
                kind="error",
                detail="boom <script> & 'quotes'",
                source="capture",
                state="",
                frame_id=None,
            ),
        ),
    )
    pdf = render_pdf(escaped, {key: None for key in IMAGE_KEYS})
    assert_valid_pdf(pdf)
    text = pdf_text(pdf)
    assert b"boom" in text
