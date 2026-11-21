"""Report PDF — the reportlab rebuild of the same report sections for
session-report ticket 03 (:mod:`seestar_controller.report.pdf`).

This is a PURE module: **no ``gi`` import** (a subprocess test enforces the
boundary) — stdlib + :mod:`reportlab` (lazy, like matplotlib in
:mod:`.plots`) + the sibling pure module :mod:`.data` only.

What lives here:

- :data:`PDF_FILE_SUFFIX` — the report PDF suffix (``.pdf``);
- :func:`pdf_path` — the PDF location: ``<project>/reports/<session_id>.pdf``
  (NEXT TO the HTML report of :mod:`.generate`);
- :func:`render_pdf` — render one :class:`~seestar_controller.report.data.ReportData`
  plus its images into a complete PDF document (bytes), rebuilding the SAME
  sections as :func:`~seestar_controller.report.html.render_html`:
  header (title + project/session/generated meta), enhanced stack thumbnail,
  session log (summary + event table), lightcurve plot, period fold, and the
  measurements summary table. Ticket 02 adds two OPTIONAL sections at the
  end, omitted when the data is missing: **Frame quality** (scored-frame
  count, median FWHM, % rejected, the FWHM-vs-JD sparkline PNG and a table
  of the flagged frames) and **Moving objects** (a table of the detections).

Documented choices:

- **reportlab rebuild, not headless-browser print-to-PDF.** The spec left the
  PDF path open (Chromium print-to-PDF vs reportlab). Chosen: **reportlab** —
  it is a pure-Python pip dependency (no external browser binary, no display,
  no subprocess), so the PDF export works identically on the desktop and in
  the headless sandbox and stays unit-testable inside the pure ``report/``
  package (this module imports no ``gi`` and the tests never touch the
  network). The PDF rebuilds the same sections from the same
  :class:`~.data.ReportData` + PNG images as the HTML, so HTML and PDF never
  diverge in content.
- **reportlab imported lazily** (inside :func:`render_pdf`, like matplotlib in
  :mod:`.plots`): importing this package stays cheap and GUI-free; reportlab
  is a declared dependency of the project (``pyproject.toml``).
- **Images.** The same PNG bytes as the HTML are embedded via reportlab's
  ``Image`` flowable (reportlab decodes PNG through Pillow), scaled to fit
  the page width keeping the aspect ratio; a missing image becomes a
  placeholder paragraph (never a crash — same policy as the HTML).
- **Text.** All dynamic text is XML-escaped before going into a
  ``Paragraph`` (reportlab paragraphs are XML markup).
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from xml.sax.saxutils import escape

from .data import LogEntry, ReportData
from .html import (
    IMAGE_FOLD,
    IMAGE_KEYS,
    IMAGE_LIGHTCURVE,
    IMAGE_QUALITY,
    IMAGE_THUMBNAIL,
    _duration_text,
    _fmt_float,
    _frame_caption,
)

#: File suffix of a session report PDF.
PDF_FILE_SUFFIX = ".pdf"

#: Maximum rendered width of an embedded plot/thumbnail (points; A4 width
#: minus margins is ~523 pt).
IMAGE_MAX_WIDTH_PT = 460.0

#: Maximum rendered height of an embedded plot/thumbnail (points).
IMAGE_MAX_HEIGHT_PT = 340.0


def pdf_path(project: Any, session_id: str) -> Path:
    """The PDF report path of ``session_id`` in ``project``:
    ``<project>/reports/<session_id>.pdf`` — next to the HTML report.

    :class:`TypeError` when ``session_id`` is not a string or ``project`` is
    not a project handle (``db``/``config``/``path``) — same checks as
    :func:`~seestar_controller.report.generate.report_path`.
    """
    if not isinstance(session_id, str):
        raise TypeError(f"session_id must be a string, got {session_id!r}")
    if not all(hasattr(project, attr) for attr in ("db", "config", "path")):
        raise TypeError(
            "project must be an open seestar_controller.store Project handle "
            f"(with db/config/path), got {type(project).__name__}"
        )
    from .generate import REPORTS_DIR

    return Path(project.path) / REPORTS_DIR / f"{session_id}{PDF_FILE_SUFFIX}"


def _reportlab():
    """Import reportlab lazily and return the pieces used by
    :func:`render_pdf` (kept lazy like matplotlib in :mod:`.plots`, so
    importing this package never pulls reportlab in)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import (
        Image,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    return SimpleNamespace(
        colors=colors,
        A4=A4,
        getSampleStyleSheet=getSampleStyleSheet,
        cm=cm,
        ImageReader=ImageReader,
        Image=Image,
        Paragraph=Paragraph,
        SimpleDocTemplate=SimpleDocTemplate,
        Spacer=Spacer,
        Table=Table,
        TableStyle=TableStyle,
    )


def _esc(value: Any) -> str:
    """XML-escape the string form of ``value`` for a reportlab Paragraph."""
    return escape(str(value))


def _image_flowable(rl: Any, styles: Any, png: bytes | None, alt: str, max_w: float, max_h: float) -> Any:
    """A reportlab ``Image`` flowable for ``png`` scaled to fit
    ``max_w`` x ``max_h`` (aspect preserved), or a placeholder Paragraph when
    ``png`` is None."""
    if png is None:
        return rl.Paragraph(f"<i>{_esc(alt)} — no image available</i>", styles["Normal"])
    reader = rl.ImageReader(io.BytesIO(png))
    iw, ih = reader.getSize()
    scale = min(max_w / iw, max_h / ih, 1.0)
    return rl.Image(io.BytesIO(png), width=iw * scale, height=ih * scale)


def _log_table(rl: Any, styles: Any, data: ReportData) -> Any:
    """The session-log event table (time, kind, source, state, detail)."""
    header = ["time (UTC)", "kind", "source", "state", "detail"]
    rows = [[rl.Paragraph(f"<b>{_esc(h)}</b>", styles["Normal"]) for h in header]]
    for entry in data.log:
        rows.append(
            [
                rl.Paragraph(_esc(entry.utc), styles["Normal"]),
                rl.Paragraph(_esc(entry.kind), styles["Normal"]),
                rl.Paragraph(_esc(entry.source), styles["Normal"]),
                rl.Paragraph(_esc(entry.state), styles["Normal"]),
                rl.Paragraph(_esc(entry.detail), styles["Normal"]),
            ]
        )
    table = rl.Table(rows, repeatRows=1)
    table.setStyle(
        rl.TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, rl.colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), rl.colors.lightgrey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return table


def _measurements_table(rl: Any, styles: Any, data: ReportData) -> Any:
    """The measurements summary table (label / value rows)."""
    s = data.summary
    rows = [
        ("measurements", str(s.n_points)),
        ("excluded (outliers)", str(s.n_excluded)),
        ("sessions with points", str(s.n_sessions)),
        ("mag min", _fmt_float(s.mag_min)),
        ("mag max", _fmt_float(s.mag_max)),
        ("mag mean", _fmt_float(s.mag_mean)),
        ("mag std", _fmt_float(s.mag_std)),
        ("JD min", _fmt_float(s.jd_min)),
        ("JD max", _fmt_float(s.jd_max)),
    ]
    if s.per_session:
        rows.append(
            ("per session", ", ".join(f"{sid}: {count}" for sid, count in s.per_session))
        )
    table = rl.Table(
        [[rl.Paragraph(f"<b>{_esc(label)}</b>", styles["Normal"]),
          rl.Paragraph(_esc(value), styles["Normal"])] for label, value in rows]
    )
    table.setStyle(
        rl.TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, rl.colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return table


def _grid_table(rl: Any, styles: Any, header: list[str], rows: list[list[str]]) -> Any:
    """A generic GRID-styled table with a bold header row (ticket 02
    sections: flagged frames, movers)."""
    table_rows = [[rl.Paragraph(f"<b>{_esc(h)}</b>", styles["Normal"]) for h in header]]
    for row in rows:
        table_rows.append([rl.Paragraph(_esc(cell), styles["Normal"]) for cell in row])
    table = rl.Table(table_rows, repeatRows=1)
    table.setStyle(
        rl.TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, rl.colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), rl.colors.lightgrey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return table


def _quality_section_flowables(rl: Any, styles: Any, data: ReportData,
                               images: Mapping[str, bytes | None]) -> list[Any]:
    """The frame-quality section flowables (ticket 02) — only called when
    ``data.quality`` is not None."""
    q = data.quality
    assert q is not None  # for ty: the caller guarantees it
    caption = (
        f"{q.frame_count} scored frame(s) · median FWHM {_fmt_float(q.median_fwhm, 2)} px · "
        f"{q.rejected_percent:.1f}% rejected ({q.flagged_count} flagged)"
    )
    out: list[Any] = [
        rl.Paragraph("Frame quality", styles["Heading2"]),
        rl.Paragraph(_esc(caption), styles["Normal"]),
        _image_flowable(rl, styles, images.get(IMAGE_QUALITY),
                         "Frame quality trend (FWHM vs JD)",
                         IMAGE_MAX_WIDTH_PT, IMAGE_MAX_HEIGHT_PT),
    ]
    flagged = q.flagged
    if flagged:
        rows = [
            [
                str(p.frame_id),
                _fmt_float(p.jd),
                _fmt_float(p.fwhm, 2),
                ", ".join(p.flag_reasons) or "—",
            ]
            for p in flagged
        ]
        out.append(_grid_table(rl, styles, ["frame", "JD", "FWHM (px)", "flag reasons"], rows))
    out.append(rl.Spacer(1, 0.3 * rl.cm))
    return out


def _movers_section_flowables(rl: Any, styles: Any, data: ReportData) -> list[Any]:
    """The moving-objects section flowables (ticket 02) — only called when
    ``data.movers`` is a non-empty tuple."""
    movers = data.movers or ()
    caption = f"{len(movers)} moving object(s) detected"
    rows = [[m.kind, m.label, m.position, m.frames] for m in movers]
    return [
        rl.Paragraph("Moving objects", styles["Heading2"]),
        rl.Paragraph(_esc(caption), styles["Normal"]),
        _grid_table(rl, styles, ["kind", "label", "position", "frames"], rows),
        rl.Spacer(1, 0.3 * rl.cm),
    ]


def render_pdf(data: ReportData, images: Mapping[str, bytes | None]) -> bytes:
    """Render ``data`` + ``images`` into a complete PDF document (bytes),
    rebuilding the same sections as
    :func:`~seestar_controller.report.html.render_html`.

    - ``images`` — a mapping with the keys :data:`~seestar_controller.report.html.IMAGE_KEYS`
      to PNG bytes (or None for a placeholder);
    - the document is A4, self-contained (images embedded, no external
      references), all dynamic text XML-escaped;
    - the optional sections (frame quality, moving objects — ticket 02) are
      appended only when the corresponding data is present;
    - returns the PDF file bytes (starting with ``%PDF-``).
    """
    for key in IMAGE_KEYS:
        if key not in images:
            raise TypeError(f"images is missing the key {key!r}")
    rl = _reportlab()
    styles = rl.getSampleStyleSheet()
    story: list[Any] = []

    target_line = f" · target {data.target}" if data.target else ""
    story.append(rl.Paragraph(_esc(f"Session report — {data.project_name}"), styles["Title"]))
    story.append(
        rl.Paragraph(
            _esc(
                f"project {data.project_slug}{target_line} · session {data.session_id} · "
                f"generated {data.generated_at}"
            ),
            styles["Normal"],
        )
    )
    story.append(rl.Spacer(1, 0.3 * rl.cm))

    # enhanced stack thumbnail
    story.append(rl.Paragraph("Enhanced stack", styles["Heading2"]))
    story.append(
        _image_flowable(rl, styles, images.get(IMAGE_THUMBNAIL), "Enhanced stack thumbnail",
                        IMAGE_MAX_WIDTH_PT, IMAGE_MAX_HEIGHT_PT)
    )
    caption = _frame_caption(data.stack_frame)
    if caption:
        story.append(rl.Paragraph(_esc(caption), styles["Normal"]))
    story.append(rl.Spacer(1, 0.3 * rl.cm))

    # session log
    counts: dict[str, int] = {}
    for frame in data.frames:
        counts[frame.kind] = counts.get(frame.kind, 0) + 1
    frames_text = ", ".join(f"{count} {kind}" for kind, count in sorted(counts.items())) or "no frames"
    story.append(rl.Paragraph("Session log", styles["Heading2"]))
    story.append(
        rl.Paragraph(
            _esc(
                f"session {data.session_id} · started {data.started_at} · "
                f"ended {data.ended_at or 'open'} · duration "
                f"{_duration_text(data.started_at, data.ended_at)} · "
                f"{len(data.frames)} frame(s) ({frames_text}) · {len(data.log)} log event(s)"
            ),
            styles["Normal"],
        )
    )
    if data.log:
        story.append(_log_table(rl, styles, data))
    else:
        story.append(rl.Paragraph("<i>No events recorded for this session.</i>", styles["Normal"]))
    story.append(rl.Spacer(1, 0.3 * rl.cm))

    # lightcurve
    summary = data.summary
    note = (
        f"{summary.n_points} measurement(s) across {summary.n_sessions} session(s)"
        + (f", {summary.n_excluded} excluded" if summary.n_excluded else "")
    )
    story.append(rl.Paragraph("Lightcurve", styles["Heading2"]))
    story.append(rl.Paragraph(_esc(note), styles["Normal"]))
    story.append(
        _image_flowable(rl, styles, images.get(IMAGE_LIGHTCURVE), "Lightcurve",
                        IMAGE_MAX_WIDTH_PT, IMAGE_MAX_HEIGHT_PT)
    )
    story.append(rl.Spacer(1, 0.3 * rl.cm))

    # period fold
    story.append(rl.Paragraph("Period fold", styles["Heading2"]))
    if data.period is None:
        story.append(
            rl.Paragraph("<i>No period selected — run period analysis first.</i>", styles["Normal"])
        )
    else:
        story.append(
            rl.Paragraph(
                _esc(f"current period: {_fmt_float(data.period)} d ({len(data.folded)} folded point(s))"),
                styles["Normal"],
            )
        )
        story.append(
            _image_flowable(rl, styles, images.get(IMAGE_FOLD), "Phase fold",
                            IMAGE_MAX_WIDTH_PT, IMAGE_MAX_HEIGHT_PT)
        )
    story.append(rl.Spacer(1, 0.3 * rl.cm))

    # measurements summary
    story.append(rl.Paragraph("Measurements summary", styles["Heading2"]))
    story.append(_measurements_table(rl, styles, data))
    story.append(rl.Spacer(1, 0.3 * rl.cm))

    # frame quality (ticket 02, optional — omitted when no scored frames)
    if data.quality is not None:
        story.extend(_quality_section_flowables(rl, styles, data, images))

    # moving objects (ticket 02, optional — omitted when no mover data)
    if data.movers:
        story.extend(_movers_section_flowables(rl, styles, data))

    buf = io.BytesIO()
    doc = rl.SimpleDocTemplate(
        buf,
        pagesize=rl.A4,
        title=f"Session report — {data.project_name} — {data.session_id}",
        author="seestar-controller",
    )
    doc.build(story)
    return buf.getvalue()


def log_entry_pdf_row(entry: LogEntry) -> tuple[str, str, str, str, str]:
    """One log entry as an escaped plain-text row (helper kept next to the
    renderer, mirroring :func:`~seestar_controller.report.html.log_entry_row`)."""
    return (entry.utc, entry.kind, entry.source, entry.state, entry.detail)
