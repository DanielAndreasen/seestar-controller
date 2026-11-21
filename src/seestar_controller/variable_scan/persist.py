"""Persistence of variable-scan results in the project db.

Pure Python — no GTK, no link, stdlib :mod:`sqlite3` only (the connection
comes with the open :class:`~seestar_controller.store.project.Project`
handle). The ``scan_results`` table is created by schema migration
**6 -> 7** (see :mod:`seestar_controller.store.schema`); this module only
reads/writes it.

API (all take the open :class:`~.project.Project` handle first):

- :func:`save_scan_results` — append one row per scanned star of a
  :class:`~.scan.ScanReport` in ONE transaction (``with project.db:``),
  returns the inserted :class:`ScanResultRecord` rows (with their db ids);
- :func:`scan_results` — ALL persisted scan rows of the project, newest run
  first (``scanned_at`` DESC), then by ``candidate_rank`` — runs accumulate
  across nights, so this is the whole history;
- :func:`latest_scan_results` — only the newest run's rows, ordered by
  ``candidate_rank`` (the ranked candidate list of the latest scan).

Documented choices:

- **Append, never replace**: every scan run appends its rows (keyed by
  ``scanned_at``); nothing is deleted except by the ``ON DELETE CASCADE``
  of the stack frame (deleting the stack deletes its scan rows).
- **Validation**: :func:`save_scan_results` checks the report type
  (:class:`TypeError` for anything but a :class:`~.scan.ScanReport`) and the
  project handle before writing; the row values come from the already
  validated report, so the insert cannot fail on bad data.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .errors import ScanError
from .scan import ScanReport
from .settings import _check_project

#: The db table holding the persisted scan results (schema v7).
SCAN_RESULTS_TABLE = "scan_results"

#: Columns selected for a :class:`ScanResultRecord`, in field order.
_SCAN_RESULTS_COLUMNS = (
    "id, scanned_at, stack_frame_id, source_id, ra_deg, dec_deg, x, y, "
    "n_frames, mad, median_err, variability, variable, candidate_rank, "
    "threshold, min_frames"
)

_INSERT_SQL = (
    f"INSERT INTO {SCAN_RESULTS_TABLE} "
    "(scanned_at, stack_frame_id, source_id, ra_deg, dec_deg, x, y, "
    "n_frames, mad, median_err, variability, variable, candidate_rank, "
    "threshold, min_frames) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


@dataclass(frozen=True)
class ScanResultRecord:
    """One persisted ``scan_results`` row (one scanned field star of one
    scan run).

    - ``id`` — the db row id;
    - ``scanned_at`` — the run time (UTC ISO-8601);
    - ``stack_frame_id`` — the stack whose source list was scanned;
    - ``source_id`` — the Gaia DR3 source id (None when unmatched);
    - ``ra_deg`` / ``dec_deg`` — the star's J2000 sky position (degrees);
    - ``x`` / ``y`` — the star's pixel position in the stack;
    - ``n_frames`` — the number of frames the star was measured on;
    - ``mad`` — the robust scatter (raw MAD of the differential mags);
    - ``median_err`` — the median photometric error;
    - ``variability`` — ``mad / median_err``;
    - ``variable`` — the candidate flag;
    - ``rank`` — the 1-based ``candidate_rank`` within the run;
    - ``threshold`` / ``min_frames`` — the scan parameters of the run.
    """

    id: int
    scanned_at: str
    stack_frame_id: int
    source_id: int | None
    ra_deg: float
    dec_deg: float
    x: float
    y: float
    n_frames: int
    mad: float
    median_err: float
    variability: float
    variable: bool
    rank: int
    threshold: float
    min_frames: int


def _row_to_record(row: sqlite3.Row | tuple[Any, ...]) -> ScanResultRecord:
    return ScanResultRecord(
        id=int(row[0]),
        scanned_at=str(row[1]),
        stack_frame_id=int(row[2]),
        source_id=None if row[3] is None else int(row[3]),
        ra_deg=float(row[4]),
        dec_deg=float(row[5]),
        x=float(row[6]),
        y=float(row[7]),
        n_frames=int(row[8]),
        mad=float(row[9]),
        median_err=float(row[10]),
        variability=float(row[11]),
        variable=bool(row[12]),
        rank=int(row[13]),
        threshold=float(row[14]),
        min_frames=int(row[15]),
    )


def save_scan_results(project: Any, report: ScanReport) -> tuple[ScanResultRecord, ...]:
    """Append the results of ``report`` (a :class:`~.scan.ScanReport`) to
    the ``scan_results`` table of ``project`` — one row per scanned star —
    in ONE transaction (``with project.db:``), and return the inserted
    :class:`ScanResultRecord` rows (with their db ids), in rank order.

    Raises :class:`TypeError` when ``project`` is not an open Project handle
    or ``report`` is not a :class:`~.scan.ScanReport`; nothing is written
    then.
    """
    project = _check_project(project)
    if not isinstance(report, ScanReport):
        raise TypeError(
            f"report must be a variable_scan ScanReport, got {type(report).__name__}"
        )
    records: list[ScanResultRecord] = []
    try:
        with project.db:  # one transaction: all inserts, or nothing
            for r in report.results:
                cur = project.db.execute(
                    _INSERT_SQL,
                    (
                        report.scanned_at,
                        report.stack_frame_id,
                        r.star.source_id,
                        r.star.ra_deg,
                        r.star.dec_deg,
                        r.star.x,
                        r.star.y,
                        r.n_frames,
                        r.mad,
                        r.median_err,
                        r.variability,
                        int(r.variable),
                        r.rank,
                        report.threshold,
                        report.min_frames,
                    ),
                )
                row_id = cur.lastrowid
                if not isinstance(row_id, int):
                    raise ScanError(
                        f"insert into {SCAN_RESULTS_TABLE} returned no row id"
                    )
                records.append(
                    ScanResultRecord(
                        id=row_id,
                        scanned_at=report.scanned_at,
                        stack_frame_id=report.stack_frame_id,
                        source_id=r.star.source_id,
                        ra_deg=r.star.ra_deg,
                        dec_deg=r.star.dec_deg,
                        x=r.star.x,
                        y=r.star.y,
                        n_frames=r.n_frames,
                        mad=r.mad,
                        median_err=r.median_err,
                        variability=r.variability,
                        variable=r.variable,
                        rank=r.rank,
                        threshold=report.threshold,
                        min_frames=report.min_frames,
                    )
                )
    except sqlite3.DatabaseError as exc:
        raise ScanError(
            f"could not save scan results in project {project.slug!r}: {exc}"
        ) from exc
    return tuple(records)


def scan_results(project: Any) -> tuple[ScanResultRecord, ...]:
    """ALL persisted scan-result rows of ``project`` as a tuple of
    :class:`ScanResultRecord`, newest run first (``scanned_at`` DESC),
    then by ``candidate_rank`` ASC (ties by row id) — the whole history,
    since runs accumulate across nights.

    Raises :class:`TypeError` when ``project`` is not an open Project handle.
    """
    project = _check_project(project)
    rows = project.db.execute(
        f"SELECT {_SCAN_RESULTS_COLUMNS} FROM {SCAN_RESULTS_TABLE} "
        "ORDER BY scanned_at DESC, candidate_rank ASC, id ASC"
    ).fetchall()
    return tuple(_row_to_record(row) for row in rows)


def latest_scan_results(project: Any) -> tuple[ScanResultRecord, ...]:
    """Only the NEWEST scan run's rows of ``project``, ordered by
    ``candidate_rank`` ASC (the ranked candidate list of the latest scan);
    an empty tuple when the project has no scan results yet.

    Raises :class:`TypeError` when ``project`` is not an open Project handle.
    """
    all_rows = scan_results(project)
    if not all_rows:
        return ()
    newest = all_rows[0].scanned_at
    return tuple(r for r in all_rows if r.scanned_at == newest)
