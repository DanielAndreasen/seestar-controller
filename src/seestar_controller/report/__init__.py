"""Session report — a self-contained HTML summary of one observing session
(``.scratch/session-report/spec.md``, ticket 01).

PURE package: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test in ``tests/test_report_boundary.py``
enforces the boundary) — stdlib + :mod:`seestar_controller.store` +
:mod:`seestar_controller.analysis` (the pure fold math) + numpy/PIL +
matplotlib (Agg, lazy) only. No :mod:`seestar_controller.link`, no
:mod:`ui`, no :mod:`photometry`, no :mod:`enhance`, no :mod:`observing`
imports. Fully unit-testable headless; the tests never touch the network.

What lives here:

- :mod:`.data` — collect the report data from an open project + session id:
  the session record, the session's frames, the session event log (the
  ``subs/<session>/session_log.jsonl`` file, read directly), the project-wide
  lightcurve, the current period from ``project.json`` with the folded
  points, the measurements summary, and the best stack frame
  (:func:`~.data.collect_report_data` -> :class:`~.data.ReportData`);
- :mod:`.plots` — the matplotlib (Agg) plots as PNG bytes:
  :func:`~.plots.lightcurve_png` (mag vs JD) and :func:`~.plots.fold_png`
  (mag vs phase, the current period/fold overlay);
- :mod:`.thumbnail` — the stack thumbnail as PNG bytes (PIL):
  :func:`~.thumbnail.stack_thumbnail_png`;
- :mod:`.html` — the single self-contained HTML document (base64-embedded
  PNGs, no external references): :func:`~.html.render_html`;
- :mod:`.generate` — :func:`~.generate.generate_report`: build + write the
  report to ``<project>/reports/<session_id>.html`` (file-based persistence,
  no db record — the store schema is owned by another workstream; regenerating
  overwrites) and return the path; plus ticket 03's
  :func:`~.generate.generate_pdf` (the PDF export of the same report, written
  next to the HTML) and :func:`~.generate.list_session_reports` (the saved
  report files of one session, for the GUI's per-session list);
- :mod:`.pdf` (ticket 03) — the reportlab rebuild of the same sections into a
  PDF document: :func:`~.pdf.render_pdf` + :func:`~.pdf.pdf_path` (reportlab
  imported lazily, like matplotlib in :mod:`.plots`).

Ticket 01 covers the pure core; the view/PDF wiring is ticket 03.
"""

from __future__ import annotations

from .data import (
    PERIOD_CONFIG_KEY,
    SESSION_LOG_FILE,
    LogEntry,
    MeasurementsSummary,
    ReportData,
    ReportError,
    collect_report_data,
    load_session_log,
    session_log_path,
    summarize_measurements,
)
from .generate import (
    REPORT_FILE_SUFFIX,
    REPORTS_DIR,
    generate_pdf,
    generate_report,
    list_session_reports,
    report_path,
)
from .html import (
    IMAGE_FOLD,
    IMAGE_KEYS,
    IMAGE_LIGHTCURVE,
    IMAGE_THUMBNAIL,
    SECTION_FOLD,
    SECTION_IDS,
    SECTION_LIGHTCURVE,
    SECTION_LOG,
    SECTION_MEASUREMENTS,
    SECTION_STACK,
    render_html,
)
from .pdf import PDF_FILE_SUFFIX, pdf_path, render_pdf
from .plots import FOLD_FIGSIZE, LIGHTCURVE_FIGSIZE, PNG_DPI, fold_png, lightcurve_png
from .thumbnail import STRETCH_PERCENTILES, THUMBNAIL_MAX_PX, stack_thumbnail_png

__all__ = [
    "FOLD_FIGSIZE",
    "IMAGE_FOLD",
    "IMAGE_KEYS",
    "IMAGE_LIGHTCURVE",
    "IMAGE_THUMBNAIL",
    "LIGHTCURVE_FIGSIZE",
    "PDF_FILE_SUFFIX",
    "PERIOD_CONFIG_KEY",
    "PNG_DPI",
    "REPORTS_DIR",
    "REPORT_FILE_SUFFIX",
    "SECTION_FOLD",
    "SECTION_IDS",
    "SECTION_LIGHTCURVE",
    "SECTION_LOG",
    "SECTION_MEASUREMENTS",
    "SECTION_STACK",
    "SESSION_LOG_FILE",
    "STRETCH_PERCENTILES",
    "THUMBNAIL_MAX_PX",
    "LogEntry",
    "MeasurementsSummary",
    "ReportData",
    "ReportError",
    "collect_report_data",
    "fold_png",
    "generate_pdf",
    "generate_report",
    "lightcurve_png",
    "list_session_reports",
    "load_session_log",
    "pdf_path",
    "render_html",
    "render_pdf",
    "report_path",
    "session_log_path",
    "stack_thumbnail_png",
    "summarize_measurements",
]
