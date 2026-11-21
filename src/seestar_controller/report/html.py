"""Report HTML — the single self-contained HTML document for session-report
ticket 01 (:mod:`seestar_controller.report.html`).

This is a PURE module: **no ``gi`` import** (a subprocess test enforces the
boundary) — stdlib (:mod:`base64`, :mod:`html`) only.

What lives here:

- :func:`render_html` — render one :class:`~seestar_controller.report.data.ReportData`
  plus its images into a SINGLE self-contained HTML string: every image is
  embedded as a ``data:image/png;base64,...`` URI, there are NO external
  references (no ``http(s)://`` src/href), and all dynamic text is escaped
  with :func:`html.escape`.

Sections (stable ``id``\\ s, used by the tests and by the later view ticket):

- ``stack-thumbnail`` — the enhanced stack thumbnail (or a placeholder when
  the session has no stack/enhanced frame or the file is unreadable), with a
  caption (frame path, JD, exposure, gain);
- ``session-log`` — the session summary line (start/end/duration, frame
  counts by kind) + the event log table (time, kind, source, state, detail),
  or a "no events recorded" note;
- ``lightcurve`` — the project-wide lightcurve plot (embedded PNG);
- ``fold`` — the current period + the phase-fold overlay plot (embedded
  PNG), or a "no period selected" note;
- ``measurements`` — the measurements summary table (counts, mag/jd stats,
  per-session counts);
- ``frame-quality`` (ticket 02, OPTIONAL) — the frame-quality summary: the
  scored-frame count, median FWHM, % rejected, the FWHM-vs-JD sparkline
  (embedded PNG) and a table of the flagged frames with their reasons. The
  section is OMITTED when the session has no scored frames;
- ``movers`` (ticket 02, OPTIONAL) — the detected moving objects (from
  :mod:`seestar_controller.moving_objects` when available) as a table. The
  section is OMITTED when there is no mover data.
"""

from __future__ import annotations

import base64
import html
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from seestar_controller.store.frames import Frame

from .data import LogEntry, ReportData

#: The image keys of :func:`render_html`'s ``images`` mapping.
IMAGE_THUMBNAIL = "thumbnail"
IMAGE_LIGHTCURVE = "lightcurve"
IMAGE_FOLD = "fold"
IMAGE_QUALITY = "quality-trend"
IMAGE_KEYS = (IMAGE_THUMBNAIL, IMAGE_LIGHTCURVE, IMAGE_FOLD, IMAGE_QUALITY)

#: The section ids of the rendered document (always present).
SECTION_STACK = "stack-thumbnail"
SECTION_LOG = "session-log"
SECTION_LIGHTCURVE = "lightcurve"
SECTION_FOLD = "fold"
SECTION_MEASUREMENTS = "measurements"
SECTION_IDS = (SECTION_STACK, SECTION_LOG, SECTION_LIGHTCURVE, SECTION_FOLD, SECTION_MEASUREMENTS)

#: The OPTIONAL section ids (ticket 02) — rendered only when the data exists:
#: ``frame-quality`` when the session has scored frames, ``movers`` when
#: moving-object detections exist.
SECTION_QUALITY = "frame-quality"
SECTION_MOVERS = "movers"
OPTIONAL_SECTION_IDS = (SECTION_QUALITY, SECTION_MOVERS)

_CSS = """
:root { color-scheme: light dark; }
body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 60rem;
       padding: 0 1rem; line-height: 1.45; }
header.meta p { color: #888; margin: 0.15rem 0; }
h1 { margin-bottom: 0.2rem; }
h2 { border-bottom: 1px solid #8884; margin-top: 2rem; padding-bottom: 0.2rem; }
table { border-collapse: collapse; width: 100%; font-size: 0.92rem; }
th, td { border: 1px solid #8886; padding: 0.25rem 0.5rem; text-align: left;
         vertical-align: top; }
th { background: #8882; }
img { max-width: 100%; height: auto; border: 1px solid #8886; border-radius: 4px; }
.placeholder { color: #888; font-style: italic; border: 1px dashed #8886;
               padding: 1rem; border-radius: 4px; }
.caption { color: #888; font-size: 0.85rem; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
"""


def _esc(value: Any) -> str:
    """``html.escape`` of the string form of ``value``."""
    return html.escape(str(value))


def _fmt_float(value: float | None, digits: int = 6) -> str:
    """Format a float for the report (``-`` when None)."""
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def _img_tag(png: bytes | None, alt: str) -> str:
    """An ``<img>`` with the PNG embedded as a base64 data URI, or a
    placeholder paragraph when ``png`` is None."""
    if png is None:
        return f'<p class="placeholder">{_esc(alt)} — no image available</p>'
    b64 = base64.b64encode(png).decode("ascii")
    return f'<img alt="{_esc(alt)}" src="data:image/png;base64,{b64}">'


def _duration_text(started_at: str, ended_at: str | None) -> str:
    """The session duration as ``H:MM:SS`` (``open`` when still running)."""
    try:
        start = datetime.fromisoformat(started_at)
    except ValueError:
        return "—"
    if ended_at is None:
        return "open"
    try:
        end = datetime.fromisoformat(ended_at)
    except ValueError:
        return "—"
    seconds = max(0, int((end - start).total_seconds()))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def _frame_caption(frame: Frame | None) -> str:
    """The caption line under the thumbnail."""
    if frame is None:
        return ""
    parts = [f"frame #{frame.id} ({frame.kind})", frame.path,
             f"JD {_fmt_float(frame.jd)}"]
    if frame.exposure is not None:
        parts.append(f"exposure {_fmt_float(frame.exposure, 3)} s")
    if frame.gain is not None:
        parts.append(f"gain {_fmt_float(frame.gain, 3)}")
    return " · ".join(parts)


def _render_stack_section(data: ReportData, images: Mapping[str, bytes | None]) -> str:
    caption = _frame_caption(data.stack_frame)
    caption_html = f'<p class="caption">{_esc(caption)}</p>' if caption else ""
    return (
        f'<section id="{SECTION_STACK}">\n<h2>Enhanced stack</h2>\n'
        f'{_img_tag(images.get(IMAGE_THUMBNAIL), "Enhanced stack thumbnail")}\n'
        f"{caption_html}\n</section>"
    )


def _render_log_section(data: ReportData) -> str:
    counts: dict[str, int] = {}
    for frame in data.frames:
        counts[frame.kind] = counts.get(frame.kind, 0) + 1
    frames_text = ", ".join(f"{count} {kind}" for kind, count in sorted(counts.items()))
    if not frames_text:
        frames_text = "no frames"
    summary = (
        f"session {_esc(data.session_id)} · started {_esc(data.started_at)} · "
        f"ended {_esc(data.ended_at or 'open')} · duration "
        f"{_esc(_duration_text(data.started_at, data.ended_at))} · "
        f"{len(data.frames)} frame(s) ({_esc(frames_text)}) · "
        f"{len(data.log)} log event(s)"
    )
    if not data.log:
        table = '<p class="placeholder">No events recorded for this session.</p>'
    else:
        rows = [log_entry_row(entry) for entry in data.log]
        table = (
            "<table>\n<thead><tr><th>time (UTC)</th><th>kind</th><th>source</th>"
            "<th>state</th><th>detail</th></tr></thead>\n<tbody>\n"
            + "\n".join(rows)
            + "\n</tbody>\n</table>"
        )
    return (
        f'<section id="{SECTION_LOG}">\n<h2>Session log</h2>\n'
        f'<p class="caption">{summary}</p>\n{table}\n</section>'
    )


def _render_lightcurve_section(data: ReportData, images: Mapping[str, bytes | None]) -> str:
    summary = data.summary
    note = (
        f"{summary.n_points} measurement(s) across {summary.n_sessions} session(s)"
        + (f", {summary.n_excluded} excluded" if summary.n_excluded else "")
    )
    return (
        f'<section id="{SECTION_LIGHTCURVE}">\n<h2>Lightcurve</h2>\n'
        f'<p class="caption">{_esc(note)}</p>\n'
        f'{_img_tag(images.get(IMAGE_LIGHTCURVE), "Lightcurve")}\n</section>'
    )


def _render_fold_section(data: ReportData, images: Mapping[str, bytes | None]) -> str:
    if data.period is None:
        body = '<p class="placeholder">No period selected — run period analysis first.</p>'
    else:
        body = (
            f'<p class="caption">current period: {_esc(_fmt_float(data.period))} d '
            f"({len(data.folded)} folded point(s))</p>\n"
            f'{_img_tag(images.get(IMAGE_FOLD), "Phase fold")}'
        )
    return f'<section id="{SECTION_FOLD}">\n<h2>Period fold</h2>\n{body}\n</section>'


def _render_measurements_section(data: ReportData) -> str:
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
    body_rows = "\n".join(
        f'<tr><th scope="row">{_esc(label)}</th><td class="num">{_esc(value)}</td></tr>'
        for label, value in rows
    )
    if s.per_session:
        per_session = ", ".join(f"{_esc(sid)}: {count}" for sid, count in s.per_session)
        body_rows += (
            f'\n<tr><th scope="row">per session</th><td class="num">{per_session}</td></tr>'
        )
    return (
        f'<section id="{SECTION_MEASUREMENTS}">\n<h2>Measurements summary</h2>\n'
        f"<table>\n<tbody>\n{body_rows}\n</tbody>\n</table>\n</section>"
    )


def _render_quality_section(data: ReportData, images: Mapping[str, bytes | None]) -> str:
    """The frame-quality section (ticket 02) — only called when
    ``data.quality`` is not None."""
    q = data.quality
    assert q is not None  # for ty: the caller guarantees it
    caption = (
        f"{q.frame_count} scored frame(s) · median FWHM {_fmt_float(q.median_fwhm, 2)} px · "
        f"{q.rejected_percent:.1f}% rejected ({q.flagged_count} flagged)"
    )
    body = f'<p class="caption">{_esc(caption)}</p>\n'
    body += _img_tag(images.get(IMAGE_QUALITY), "Frame quality trend (FWHM vs JD)")
    flagged = q.flagged
    if flagged:
        rows = "\n".join(
            "<tr>"
            f'<td class="num">{p.frame_id}</td>'
            f'<td class="num">{_fmt_float(p.jd)}</td>'
            f'<td class="num">{_fmt_float(p.fwhm, 2)}</td>'
            f"<td>{_esc(', '.join(p.flag_reasons) or '—')}</td>"
            "</tr>"
            for p in flagged
        )
        body += (
            "\n<table>\n<thead><tr><th>frame</th><th>JD</th><th>FWHM (px)</th>"
            "<th>flag reasons</th></tr></thead>\n<tbody>\n"
            + rows
            + "\n</tbody>\n</table>"
        )
    return f'<section id="{SECTION_QUALITY}">\n<h2>Frame quality</h2>\n{body}\n</section>'


def _render_movers_section(data: ReportData) -> str:
    """The moving-objects section (ticket 02) — only called when
    ``data.movers`` is a non-empty tuple."""
    movers = data.movers or ()
    caption = f"{len(movers)} moving object(s) detected"
    rows = "\n".join(
        "<tr>"
        f"<td>{_esc(m.kind)}</td>"
        f"<td>{_esc(m.label)}</td>"
        f"<td>{_esc(m.position)}</td>"
        f"<td>{_esc(m.frames)}</td>"
        "</tr>"
        for m in movers
    )
    table = (
        "<table>\n<thead><tr><th>kind</th><th>label</th><th>position</th>"
        "<th>frames</th></tr></thead>\n<tbody>\n"
        + rows
        + "\n</tbody>\n</table>"
    )
    return (
        f'<section id="{SECTION_MOVERS}">\n<h2>Moving objects</h2>\n'
        f'<p class="caption">{_esc(caption)}</p>\n{table}\n</section>'
    )


def render_html(data: ReportData, images: Mapping[str, bytes | None]) -> str:
    """Render ``data`` + ``images`` into the single self-contained HTML
    document (a string).

    - ``images`` — a mapping with the keys :data:`IMAGE_THUMBNAIL` /
      :data:`IMAGE_LIGHTCURVE` / :data:`IMAGE_FOLD` / :data:`IMAGE_QUALITY`
      (see :data:`IMAGE_KEYS`) to PNG bytes (or None for a placeholder);
    - every image is embedded as a base64 ``data:`` URI — the document has no
      external references;
    - all dynamic text is HTML-escaped;
    - the optional sections (see :data:`OPTIONAL_SECTION_IDS`) are rendered
      only when the corresponding data is present: the frame-quality section
      when ``data.quality`` is not None, the movers section when
      ``data.movers`` is a non-empty tuple.
    """
    for key in IMAGE_KEYS:
        if key not in images:
            raise TypeError(f"images is missing the key {key!r}")
    target_line = f" · target {_esc(data.target)}" if data.target else ""
    header = (
        "<header class=\"meta\">\n"
        f"<h1>Session report — {_esc(data.project_name)}</h1>\n"
        f"<p>project {_esc(data.project_slug)}{target_line}</p>\n"
        f"<p>session {_esc(data.session_id)}</p>\n"
        f"<p>generated {_esc(data.generated_at)}</p>\n"
        "</header>"
    )
    sections = "\n".join(
        (
            _render_stack_section(data, images),
            _render_log_section(data),
            _render_lightcurve_section(data, images),
            _render_fold_section(data, images),
            _render_measurements_section(data),
            *(
                (_render_quality_section(data, images),)
                if data.quality is not None
                else ()
            ),
            *((_render_movers_section(data),) if data.movers else ()),
        )
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f"<title>Session report — {_esc(data.project_name)} — {_esc(data.session_id)}</title>\n"
        f"<style>{_CSS}</style>\n</head>\n<body>\n{header}\n{sections}\n</body>\n</html>\n"
    )


def log_entry_row(entry: LogEntry) -> str:
    """One log entry as an escaped HTML table row (helper for the view
    ticket; kept next to the renderer)."""
    return (
        "<tr>"
        f"<td>{_esc(entry.utc)}</td>"
        f"<td>{_esc(entry.kind)}</td>"
        f"<td>{_esc(entry.source)}</td>"
        f"<td>{_esc(entry.state)}</td>"
        f"<td>{_esc(entry.detail)}</td>"
        "</tr>"
    )
