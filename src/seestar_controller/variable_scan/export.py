"""CSV export of the variable-candidate list — the PURE layer of
variable-scan ticket 02 (:mod:`seestar_controller.variable_scan.export`).

Pure Python: **no ``gi`` import** (a subprocess test enforces the boundary) —
stdlib (:mod:`csv`, :mod:`os`, :mod:`tempfile`, :mod:`pathlib`) +
:mod:`seestar_controller.store` + the sibling pure modules only. The widget
layer (:mod:`.view`) imports ``gi`` at module top and calls into this module.

What lives here:

- :data:`CANDIDATES_CSV_FILE` / :data:`CANDIDATES_CSV_HEADER` — the default
  destination (``<project>/variable_candidates.csv``) and the header;
- :func:`export_candidates_csv` — write the ranked candidate records (the
  displayed candidate list of the latest scan run) as CSV, one row per
  candidate, in rank order.

Documented choices:

- **Format follows the analysis exporter** (:mod:`seestar_controller.analysis.export`
  / :mod:`seestar_controller.store.csv_export`): ``repr(float)`` for the
  float columns (exact round-trip), an empty field for a NULL
  ``source_id``, ``1``/``0`` for the ``variable`` flag, ``\n`` line
  terminator, and an **atomic write** (temp file in the SAME directory +
  flush + fsync + :func:`os.replace`, temp removed on any failure) — the
  private helpers are re-declared here, exactly like
  :mod:`seestar_controller.analysis.export` re-declares them (a package
  must not import another package's private helpers).
- **Errors**: :class:`TypeError` for a non-Project handle, a non-sequence
  ``records``, a non-:class:`~.persist.ScanResultRecord` item, or a bad
  ``path``; :class:`~.errors.ScanError` for a store failure while resolving
  the default destination; :class:`OSError` from the write itself propagates
  (the previous file stays intact).
"""

from __future__ import annotations

import csv
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .errors import ScanError
from .persist import ScanResultRecord
from .settings import _check_project

#: File name of the default candidates export inside a project directory.
CANDIDATES_CSV_FILE = "variable_candidates.csv"

#: The candidates CSV header columns, in order.
CANDIDATES_CSV_HEADER = (
    "rank",
    "source_id",
    "ra_deg",
    "dec_deg",
    "x",
    "y",
    "n_frames",
    "mad",
    "median_err",
    "variability",
    "variable",
    "scanned_at",
    "threshold",
    "min_frames",
)


def _record_row(record: ScanResultRecord) -> list[str]:
    """The CSV row of one :class:`ScanResultRecord` (header order)."""
    return [
        str(record.rank),
        "" if record.source_id is None else str(record.source_id),
        repr(float(record.ra_deg)),
        repr(float(record.dec_deg)),
        repr(float(record.x)),
        repr(float(record.y)),
        str(record.n_frames),
        repr(float(record.mad)),
        repr(float(record.median_err)),
        repr(float(record.variability)),
        "1" if record.variable else "0",
        record.scanned_at,
        repr(float(record.threshold)),
        str(record.min_frames),
    ]


def _atomic_write_csv(path: Path, rows: Iterable[list[str]]) -> Path:
    """Write ``rows`` (each a list of string cells) to ``path`` atomically:
    temp file in the SAME directory + flush + fsync + :func:`os.replace`,
    temp removed on any failure (incl. :class:`KeyboardInterrupt`) — a crash
    never leaves a partial CSV and no ``*.tmp`` is left behind. Re-declared
    from the store/analysis exporters (private helpers are not shared across
    packages)."""
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            for row in rows:
                writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def export_candidates_csv(
    project: Any,
    records: Any,
    path: str | Path | None = None,
) -> Path:
    """Export the variable-candidate ``records`` (an iterable of
    :class:`~.persist.ScanResultRecord`, e.g. the displayed candidate list of
    the latest scan run, in rank order) to a CSV file and return the written
    path.

    - ``path`` — the destination; the default is
      ``<project>/variable_candidates.csv`` (:data:`CANDIDATES_CSV_FILE`).
    - the file has the :data:`CANDIDATES_CSV_HEADER` header row plus one row
      per record (rank order preserved), written atomically (see
      :func:`_atomic_write_csv`).

    Raises :class:`TypeError` for a non-Project ``project``, a non-iterable
    ``records``, a non-:class:`ScanResultRecord` item, or a bad ``path``;
    :class:`~.errors.ScanError` when the default destination cannot be
    resolved; :class:`OSError` when the write itself fails (the previous
    file stays intact).
    """
    project = _check_project(project)
    if isinstance(records, (str, bytes)) or not isinstance(records, Iterable):
        raise TypeError(
            f"records must be an iterable of ScanResultRecord, got {records!r}"
        )
    rows: list[list[str]] = [list(CANDIDATES_CSV_HEADER)]
    for record in records:
        if not isinstance(record, ScanResultRecord):
            raise TypeError(
                "records must be ScanResultRecord items, got "
                f"{type(record).__name__}"
            )
        rows.append(_record_row(record))
    if path is None:
        destination = project.path / CANDIDATES_CSV_FILE
    else:
        if not isinstance(path, (str, Path)):
            raise TypeError(f"path must be a string, Path, or None, got {path!r}")
        destination = Path(path)
    try:
        return _atomic_write_csv(destination, rows)
    except OSError:
        raise
    except Exception as exc:
        raise ScanError(
            f"could not export the candidates CSV of project {project.slug!r}: {exc}"
        ) from exc


__all__ = [
    "CANDIDATES_CSV_FILE",
    "CANDIDATES_CSV_HEADER",
    "export_candidates_csv",
]
