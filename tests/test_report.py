"""Pure-layer tests for session-report ticket 01 — the report core
(:mod:`seestar_controller.report`): ``generate_report(project, session_id)``
writes a single self-contained HTML file (base64-embedded PNGs) into
``<project>/reports/<session_id>.html`` with the sections: enhanced stack
thumbnail, session log table, lightcurve + current period/fold overlay,
measurements summary.

Headless: no ``gi`` anywhere (the sandbox has no GTK, see
docs/wiki/assumptions.md), and NO NETWORK — one test monkeypatches
:mod:`socket` to raise, proving the generator is fully offline. The
subprocess/source-scan package boundary lives in
``tests/test_report_boundary.py``.

Covered: a synthetic temp project with one session (sub-frames +
measurements, a stack, an enhanced frame, a session_log.jsonl, a persisted
period) -> the HTML contains all sections + embedded PNG images (each
decoding to a real PNG), the session log rows, the measurement values, the
period; regeneration after adding data overwrites the same path and updates
the content; unknown session / bad argument types; sessions without a stack
or without a period (placeholders, still a valid report); corrupt log lines
skipped; corrupt persisted period ignored; HTML escaping of dynamic text;
the atomic write (no ``*.tmp`` left); the data collector's fields (fold
phases in [0, 1), enhanced preferred over stack for the thumbnail); the plot
/ thumbnail / HTML helpers on their own; the frozen clock.
"""

from __future__ import annotations

import base64
import io
import json
import math
import re
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits as pyfits

from seestar_controller.report import (
    IMAGE_FOLD,
    IMAGE_KEYS,
    IMAGE_LIGHTCURVE,
    IMAGE_QUALITY,
    IMAGE_THUMBNAIL,
    REPORTS_DIR,
    SECTION_FOLD,
    SECTION_IDS,
    SECTION_LIGHTCURVE,
    SECTION_LOG,
    SECTION_MEASUREMENTS,
    SECTION_STACK,
    LogEntry,
    ReportData,
    ReportError,
    collect_report_data,
    fold_png,
    generate_report,
    lightcurve_png,
    load_session_log,
    render_html,
    report_path,
    session_log_path,
    stack_thumbnail_png,
    summarize_measurements,
)
from seestar_controller.report import generate as generate_module
from seestar_controller.report import html as html_module
from seestar_controller.report import thumbnail as thumbnail_module
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
    """Write a session_log.jsonl (dicts become JSON lines, other entries are
    written verbatim — e.g. a corrupt line)."""
    path = session_log_path(project, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e) if isinstance(e, dict) else str(e) for e in events]
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
    period 0.5 d), one stack, one enhanced frame, a session log (with one
    corrupt line), and a persisted period."""
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
            "corrupt line {{{",
            {
                "ts": (T0 + timedelta(minutes=6)).timestamp(),
                "kind": "error",
                "detail": "download failed",
                "source": "capture",
                "state": "",
                "frame_id": None,
            },
        ],
    )
    project.config["period"] = PERIOD
    project.save_config()
    return sid


def _embedded_pngs(text: str) -> list[bytes]:
    """All base64-embedded PNG payloads of the HTML document."""
    payloads = re.findall(r'data:image/png;base64,([A-Za-z0-9+/=]+)', text)
    return [base64.b64decode(p) for p in payloads]


# --- generate_report: the self-contained HTML with all sections -------------------


def test_generate_report_writes_self_contained_html_with_all_sections_and_embedded_pngs(
    project, session
):
    path = generate_report(project, session)
    assert path == report_path(project, session)
    assert path == project.path / REPORTS_DIR / f"{session}.html"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    # all sections present
    for section_id in SECTION_IDS:
        assert f'id="{section_id}"' in text, section_id
    assert f'id="{SECTION_STACK}"' in text
    assert f'id="{SECTION_LOG}"' in text
    assert f'id="{SECTION_LIGHTCURVE}"' in text
    assert f'id="{SECTION_FOLD}"' in text
    assert f'id="{SECTION_MEASUREMENTS}"' in text
    # three embedded images (thumbnail + lightcurve + fold), each a real PNG
    pngs = _embedded_pngs(text)
    assert len(pngs) == 3
    for png in pngs:
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(png) > 500  # a real plot/thumbnail, not a 1x1 stub
    # self-contained: no external references at all
    assert not re.search(r'(src|href)\s*=\s*"https?://', text)
    assert "url(http" not in text
    # header + content
    assert "M 42 night" in text
    assert session in text
    assert "session " + session + " opened" in text  # a log row
    assert "download failed" in text  # the error log row
    assert "corrupt" not in text  # the corrupt line was skipped
    assert "stack finished" in text
    assert f"{PERIOD:.6f}" in text  # the current period in the fold section
    assert "12.000000" in text  # a measurement value in the summary
    assert "8 measurement(s)" in text
    assert "enhanced" in text  # the thumbnail caption names the enhanced frame


def test_regeneration_overwrites_the_same_path_and_updates_the_content(project, session):
    path = generate_report(project, session)
    first = path.read_text(encoding="utf-8")
    # more data arrives: one more frame + a distinctive measurement
    frame = store_frame(project, session, KIND_SUB, fits_bytes(date_obs_str(1200.0), seed=30))
    add_measurement(project, frame.id, 99.123, jd=JD0 + 0.2, mag_err=0.05)
    second_path = generate_report(project, session)
    assert second_path == path
    second = path.read_text(encoding="utf-8")
    assert second != first
    assert "99.123000" in second
    assert "99.123000" not in first
    assert "9 measurement(s)" in second
    # still exactly one report file, and no temp files left behind
    reports = sorted((project.path / REPORTS_DIR).iterdir())
    assert reports == [path]
    assert not list((project.path / REPORTS_DIR).glob("*.tmp"))


def test_generate_report_uses_the_injected_clock(project, session):
    stamp = datetime(2026, 12, 1, 8, 30, 0, tzinfo=UTC)
    path = generate_report(project, session, clock=frozen(stamp))
    assert stamp.isoformat(timespec="milliseconds") in path.read_text(encoding="utf-8")


def test_generate_report_unknown_session_raises_report_error(project):
    with pytest.raises(ReportError, match="has no session 'nope'"):
        generate_report(project, "nope")
    with pytest.raises(TypeError, match="session_id must be a string"):
        generate_report(project, 123)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="project must be an open"):
        generate_report(object(), "x")  # type: ignore[arg-type]
    assert not (project.path / REPORTS_DIR).exists()  # nothing was written


def test_report_path_type_errors(project):
    assert report_path(project, "abc") == project.path / REPORTS_DIR / "abc.html"
    with pytest.raises(TypeError, match="session_id must be a string"):
        report_path(project, None)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="project must be an open"):
        report_path("not-a-project", "abc")  # type: ignore[arg-type]


def test_report_without_stack_or_period_still_generates(project):
    sid = start_session(project, clock=frozen(T0))
    frame = store_frame(project, sid, KIND_SUB, fits_bytes(date_obs_str(0.0)))
    add_measurement(project, frame.id, 12.5, jd=JD0, mag_err=0.01)
    close_session(project, sid, clock=frozen(T0 + timedelta(minutes=30)))
    path = generate_report(project, sid)
    text = path.read_text(encoding="utf-8")
    for section_id in SECTION_IDS:
        assert f'id="{section_id}"' in text, section_id
    # no stack -> thumbnail placeholder; no period -> fold placeholder
    assert "no image available" in text
    assert "No period selected" in text
    # the lightcurve plot is still embedded
    pngs = _embedded_pngs(text)
    assert len(pngs) == 1
    assert pngs[0].startswith(b"\x89PNG\r\n\x1a\n")
    assert "No events recorded" in text  # no session log file
    assert "1 measurement(s)" in text


def test_generate_report_never_touches_the_network(project, session, monkeypatch):
    def _no_network(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(socket, "getaddrinfo", _no_network)
    path = generate_report(project, session)
    assert path.is_file()
    assert len(_embedded_pngs(path.read_text(encoding="utf-8"))) == 3


def test_html_escapes_dynamic_text(root):
    p = create_project("X <script>alert(1)</script>", root=root)
    try:
        sid = start_session(p, clock=frozen(T0))
        frame = store_frame(p, sid, KIND_SUB, fits_bytes(date_obs_str(0.0)))
        add_measurement(p, frame.id, 12.0, jd=JD0)
        close_session(p, sid, clock=frozen(T0 + timedelta(minutes=5)))
        write_session_log(p, sid, [{"ts": T0.timestamp(), "kind": "error",
                                    "detail": "<b>bold</b> & co", "source": "capture",
                                    "state": "", "frame_id": None}])
        text = generate_report(p, sid).read_text(encoding="utf-8")
        assert "<script>alert(1)</script>" not in text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
        assert "<b>bold</b>" not in text
        assert "&lt;b&gt;bold&lt;/b&gt; &amp; co" in text
    finally:
        p.close()


# --- collect_report_data ------------------------------------------------------------


def test_collect_report_data_fields(project, session):
    data = collect_report_data(project, session, clock=frozen(T0 + timedelta(days=2)))
    assert isinstance(data, ReportData)
    assert data.project_name == "M 42 night"
    assert data.project_slug == "m-42-night"
    assert data.session_id == session
    assert data.started_at == T0.isoformat(timespec="milliseconds")
    assert data.ended_at == (T0 + timedelta(hours=1)).isoformat(timespec="milliseconds")
    assert data.generated_at == (T0 + timedelta(days=2)).isoformat(timespec="milliseconds")
    assert data.target == "M 42"
    # frames of the session only, all kinds, jd-ordered
    assert {f.kind for f in data.frames} == {KIND_SUB, KIND_STACK, KIND_ENHANCED}
    assert all(f.session_id == session for f in data.frames)
    # the thumbnail frame prefers the enhanced frame over the stack
    assert data.stack_frame is not None
    assert data.stack_frame.kind == KIND_ENHANCED
    # the log: 3 valid entries, the corrupt line skipped
    assert len(data.log) == 3
    assert all(isinstance(e, LogEntry) for e in data.log)
    assert data.log[0].kind == "session"
    assert data.log[2].kind == "error"
    # the lightcurve: all 8 points, excluded included
    assert len(data.lightcurve) == 8
    # the period from project.json + the fold
    assert data.period == PERIOD
    assert len(data.folded) == 8
    phases = [fp.phase for fp in data.folded]
    assert phases == sorted(phases)
    assert all(0.0 <= ph < 1.0 for ph in phases)
    # the summary
    s = data.summary
    assert s.n_points == 8
    assert s.n_excluded == 0
    assert s.n_sessions == 1
    assert s.per_session == ((session, 8),)
    assert s.mag_min == pytest.approx(min(m.mag for m in data.lightcurve))
    assert s.mag_max == pytest.approx(max(m.mag for m in data.lightcurve))
    assert s.jd_min == pytest.approx(JD0)
    assert s.jd_max == pytest.approx(JD0 + 0.07)


def test_collect_report_data_prefers_stack_when_no_enhanced(project):
    sid = start_session(project, clock=frozen(T0))
    store_frame(project, sid, KIND_STACK, fits_bytes(date_obs_str(10.0), seed=1))
    store_frame(project, sid, KIND_STACK, fits_bytes(date_obs_str(20.0), seed=2))
    close_session(project, sid, clock=frozen(T0 + timedelta(minutes=5)))
    data = collect_report_data(project, sid)
    assert data.stack_frame is not None
    assert data.stack_frame.kind == KIND_STACK
    assert data.stack_frame.jd > data.frames[0].jd  # the LATEST stack
    assert data.period is None
    assert data.folded == ()


def test_collect_report_data_ignores_a_corrupt_persisted_period(project, session):
    project.config["period"] = "not-a-number"
    project.save_config()
    data = collect_report_data(project, session)
    assert data.period is None
    assert data.folded == ()
    # and the report still generates, with the fold placeholder
    text = generate_report(project, session).read_text(encoding="utf-8")
    assert "No period selected" in text


def test_collect_report_data_unknown_session_and_bad_types(project):
    with pytest.raises(ReportError, match="has no session"):
        collect_report_data(project, "missing")
    with pytest.raises(TypeError, match="session_id must be a string"):
        collect_report_data(project, 42)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="project must be an open"):
        collect_report_data(None, "x")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_load_session_log_missing_file_and_corrupt_lines(project, session):
    assert load_session_log(project, "no-such-session") == ()
    entries = load_session_log(project, session)
    assert len(entries) == 3
    assert entries[0].utc.startswith("2026-11-20T22:00:00")
    # a file with only corrupt lines -> ()
    write_session_log(project, session, ["not json", '{"no_ts": 1}', ""])
    assert load_session_log(project, session) == ()


def test_summarize_measurements(project, session):
    data = collect_report_data(project, session)
    s = summarize_measurements(data.lightcurve)
    assert s.n_points == 8
    assert s.mag_mean == pytest.approx(sum(m.mag for m in data.lightcurve) / 8)
    empty = summarize_measurements(())
    assert empty.n_points == 0
    assert empty.mag_min is None and empty.jd_max is None and empty.per_session == ()


# --- plots / thumbnail / html helpers -------------------------------------------------


def test_plots_render_valid_pngs_for_full_and_empty_inputs(project, session):
    data = collect_report_data(project, session)
    lc = lightcurve_png(data.lightcurve)
    assert data.period is not None
    fold = fold_png(data.folded, data.period)
    for png in (lc, fold):
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(png) > 500
    # empty inputs still render a valid PNG
    assert lightcurve_png([]).startswith(b"\x89PNG\r\n\x1a\n")
    assert fold_png([], 1.0).startswith(b"\x89PNG\r\n\x1a\n")


def test_stack_thumbnail_png(project, session):
    data = collect_report_data(project, session)
    png = stack_thumbnail_png(project, data.stack_frame)
    assert png is not None
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 200
    # None frame -> None; a missing file -> None (never raises)
    assert stack_thumbnail_png(project, None) is None
    missing = store_frame(project, session, KIND_STACK, fits_bytes(date_obs_str(999.0), seed=9))
    (project.path / missing.path).unlink()
    assert stack_thumbnail_png(project, missing) is None
    with pytest.raises(TypeError, match="max_px must be a positive int"):
        stack_thumbnail_png(project, data.stack_frame, max_px=0)


def test_thumbnail_png_from_array_downscales():
    arr = np.zeros((1024, 512), dtype=np.float64)
    arr[256:768, 128:384] = 1000.0
    png = thumbnail_module.thumbnail_png_from_array(arr, max_px=256)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    from PIL import Image

    with Image.open(io.BytesIO(png)) as img:
        assert img.size == (128, 256)  # (W, H): 512x1024 downscaled to long side 256
    # flat array -> mid-gray, still a valid PNG
    flat = thumbnail_module.thumbnail_png_from_array(np.zeros((8, 8)))
    assert flat.startswith(b"\x89PNG\r\n\x1a\n")


def test_render_html_requires_all_image_keys(project, session):
    data = collect_report_data(project, session)
    with pytest.raises(TypeError, match="missing the key"):
        render_html(data, {IMAGE_THUMBNAIL: None, IMAGE_LIGHTCURVE: None})  # type: ignore[arg-type]
    doc = render_html(
        data,
        {
            IMAGE_THUMBNAIL: b"\x89PNG-fake",
            IMAGE_LIGHTCURVE: b"\x89PNG-fake",
            IMAGE_FOLD: None,
            IMAGE_QUALITY: None,
        },
    )
    assert doc.startswith("<!DOCTYPE html>")
    assert doc.count("data:image/png;base64,") == 2
    assert "No period selected" not in doc  # a period IS selected here
    assert IMAGE_KEYS == (IMAGE_THUMBNAIL, IMAGE_LIGHTCURVE, IMAGE_FOLD, IMAGE_QUALITY)
    # the module-level constants the view ticket will rely on
    assert html_module.SECTION_IDS == SECTION_IDS
    assert generate_module.REPORTS_DIR == REPORTS_DIR


def test_generate_report_is_the_generate_module_function():
    from seestar_controller import report

    assert report.generate_report is generate_module.generate_report
    assert issubclass(report.ReportError, ValueError)
    for name in report.__all__:
        assert hasattr(report, name), name
