"""Report generation — write the self-contained HTML report of one session
into the project (session-report ticket 01,
:mod:`seestar_controller.report.generate`).

This is a PURE module: **no ``gi`` import** (a subprocess test enforces the
boundary) — stdlib + the sibling pure modules :mod:`.data` / :mod:`.plots` /
:mod:`.thumbnail` / :mod:`.html` only.

What lives here:

- :data:`REPORTS_DIR` / :data:`REPORT_FILE_SUFFIX` — the report location:
  ``<project>/reports/<session_id>.html``;
- :func:`report_path` — that path for a project + session id;
- :func:`generate_report` — build the report (data + plots + thumbnail +
  HTML) and write it ATOMICALLY to :func:`report_path`, returning the path.

Documented choices:

- **File-based persistence only.** The report lives at
  ``reports/<session_id>.html`` inside the project directory. There is NO db
  record: the store schema (``sessions``/``frames``/``stars``/``measurements``/
  ``presets``) has no reports table and the schema version is NOT bumped by
  this ticket (another workstream owns the store schema) — the file under
  ``reports/`` is the record, discoverable by listing the directory.
- **Regenerating overwrites.** :func:`generate_report` writes the same path
  every time (temp file in the same directory + fsync + :func:`os.replace`,
  the repo's atomic-write pattern, re-declared here because this package
  must not import the store's private helpers), so a regenerate after more
  data arrived simply replaces the previous report; no ``*.tmp`` is left
  behind and a crash never leaves a partial report.
- **Errors.** :class:`TypeError` for bad argument types (``project`` not a
  project handle, ``session_id`` not a string); :class:`~.data.ReportError`
  for an unknown session or a store read failure. :class:`OSError` from the
  write itself propagates (the previous report stays intact).
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .data import collect_report_data
from .html import (
    IMAGE_FOLD,
    IMAGE_LIGHTCURVE,
    IMAGE_THUMBNAIL,
    render_html,
)
from .plots import fold_png, lightcurve_png
from .thumbnail import stack_thumbnail_png

#: Directory for the generated reports inside a project (``reports/``).
REPORTS_DIR = "reports"

#: File suffix of a session report.
REPORT_FILE_SUFFIX = ".html"


def report_path(project: Any, session_id: str) -> Path:
    """The report path of ``session_id`` in ``project``:
    ``<project>/reports/<session_id>.html``.

    :class:`TypeError` when ``session_id`` is not a string or ``project`` is
    not a project handle (``db``/``config``/``path``).
    """
    if not isinstance(session_id, str):
        raise TypeError(f"session_id must be a string, got {session_id!r}")
    if not all(hasattr(project, attr) for attr in ("db", "config", "path")):
        raise TypeError(
            "project must be an open seestar_controller.store Project handle "
            f"(with db/config/path), got {type(project).__name__}"
        )
    return Path(project.path) / REPORTS_DIR / f"{session_id}{REPORT_FILE_SUFFIX}"


def _atomic_write_text(path: Path, text: str) -> Path:
    """Write ``text`` (UTF-8) to ``path`` atomically: temp file in the SAME
    directory + flush + fsync + :func:`os.replace`, temp removed on any
    failure (incl. :class:`KeyboardInterrupt`) — the repo's atomic-write
    pattern (see :mod:`seestar_controller.store.project`), re-declared here
    because this package must not import the store's private helpers.
    Returns the destination path."""
    data = text.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.close(fd)  # already closed when the with-block exited
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def generate_report(
    project: Any,
    session_id: str,
    *,
    clock: Callable[[], datetime] | None = None,
) -> Path:
    """Generate the self-contained HTML report of ``session_id`` in
    ``project`` and return its path (``<project>/reports/<session_id>.html``).

    Collects the report data (:func:`~seestar_controller.report.data.collect_report_data`),
    renders the plots (matplotlib Agg) and the stack thumbnail (PIL), embeds
    them as base64 PNGs into one HTML document
    (:func:`~seestar_controller.report.html.render_html`), and writes it
    atomically to :func:`report_path` — regenerating OVERWRITES the previous
    report at the same path.

    - ``project`` — an open store project handle; :class:`TypeError` otherwise;
    - ``session_id`` — a string; :class:`TypeError` otherwise,
      :class:`~seestar_controller.report.data.ReportError` when the project
      has no such session;
    - ``clock`` — injectable clock for the report's ``generated_at``
      timestamp (tests pass a frozen clock).

    :class:`OSError` from the write itself propagates (the previous report
    stays intact).
    """
    data = collect_report_data(project, session_id, clock=clock)
    images: dict[str, bytes | None] = {
        IMAGE_THUMBNAIL: stack_thumbnail_png(project, data.stack_frame),
        IMAGE_LIGHTCURVE: lightcurve_png(
            data.lightcurve, title=f"Lightcurve — {data.project_name}"
        ),
        IMAGE_FOLD: (
            fold_png(data.folded, data.period, title=f"Phase fold — P = {data.period:.6g} d")
            if data.period is not None
            else None
        ),
    }
    document = render_html(data, images)
    return _atomic_write_text(report_path(project, session_id), document)
