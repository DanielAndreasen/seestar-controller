"""CSV export — the lightcurve of a project as ``lightcurve.csv``.

Pure-Python (module boundary per ``.scratch/project-store/spec.md``): no GTK
imports, no :mod:`seestar_controller.link` imports, stdlib only (:mod:`csv`,
:mod:`os`, :mod:`tempfile`).

The **Lightcurve** (GLOSSARY.md) of a project — all measurements across all
sessions — is exported as a CSV file so it can be loaded into other tools
(spreadsheets, plotting, period analysis). The default destination is
``<project>/lightcurve.csv`` (the export artifact reserved by the spec), an
explicit ``path`` overrides it.

API (takes the open :class:`~.project.Project` handle first):

- :func:`export_csv` — write the lightcurve CSV atomically, returns the path;
- :func:`target_name` — the ``target`` column value of a project (config
  target, falling back to the saved target star pick's label).

File format (documented choices):

- **Header** — exactly ``jd,utc,mag,mag_err,session,target`` (the
  :data:`CSV_HEADER` columns in this order), UTF-8, ``\\n`` line terminator
  (:class:`csv.writer` with ``lineterminator="\\n"`` — standard tools parse it
  fine and the raw first line reads exactly as the header above).
- **Rows** — one per :func:`~.measurements.lightcurve` point, in the same
  order (by ``jd``, ties by row id): ``jd`` as ``repr(float)`` (round-trips
  exactly through ``float()``), ``utc`` as the stored ISO-8601 string,
  ``mag`` as ``repr(float)``, ``mag_err`` as ``repr(float)`` or EMPTY when
  NULL, ``session`` as the session id, ``target`` as :func:`target_name` —
  the project config's target name, falling back to the saved target star
  pick's label when the config target is null (issue 18), empty string when
  neither is set. Fields containing commas/quotes are
  quoted by the :mod:`csv` module, so re-parsing with :class:`csv.reader`
  yields the exact original values. Issue 13: :func:`~.measurements.lightcurve`
  returns at most ONE row per frame (the newest), so the exported ``jd``
  column never contains duplicate rows from a re-run of photometry.
- **Exclusion policy: OMIT.** Points flagged ``excluded`` are NOT written by
  default — the exported file is the clean lightcurve; pass
  ``include_excluded=True`` to write them too (the same semantics as
  :func:`~.measurements.lightcurve`). Omitting (rather than marking with a
  flag column) keeps the file directly plottable; the db keeps the full
  history including outliers.
- **Atomic write** — temp file in the SAME directory + flush + fsync +
  :func:`os.replace`, temp removed on any failure incl.
  :class:`KeyboardInterrupt` (the same pattern as
  :meth:`.project.Project.save_config` and ``link/frames.py``, re-declared
  here because this package must not import them): a crash never leaves a
  partial CSV and no ``*.tmp`` file is left behind.
"""

from __future__ import annotations

import csv
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .measurements import Measurement, lightcurve
from .project import Project
from .stars import ROLE_TARGET, load_star_picks

#: File name of the default export inside a project directory.
CSV_FILE = "lightcurve.csv"

#: The CSV header columns, in order — the first line of every export.
CSV_HEADER = ("jd", "utc", "mag", "mag_err", "session", "target")


def target_name(project: Project) -> str:
    """The ``target`` column value of ``project``.

    The project config's ``target`` (``config['target']``) when set: a
    mapping's ``name`` entry, a plain string as-is, else ``""``. When the
    config target is null, the saved target star pick's label
    (:func:`~.stars.load_star_picks`, role :data:`~.stars.ROLE_TARGET`)
    falls back (issue 18) — ``""`` when the pick is unnamed or no target
    pick is saved.
    """
    target = project.config.get("target")
    if target is not None:
        if isinstance(target, str):
            return target
        if isinstance(target, Mapping):
            name = target.get("name")
            return "" if name is None else str(name)
        return ""
    for pick in load_star_picks(project):
        if pick.role == ROLE_TARGET:
            return pick.label if pick.label is not None else ""
    return ""


def _format_row(measurement: Measurement, target_name: str) -> list[str]:
    """One lightcurve point as a CSV row of strings (see the module
    docstring): ``repr`` floats round-trip exactly, NULL ``mag_err`` ->
    empty field."""
    return [
        repr(float(measurement.jd)),
        measurement.utc,
        repr(float(measurement.mag)),
        "" if measurement.mag_err is None else repr(float(measurement.mag_err)),
        measurement.session_id,
        target_name,
    ]


def export_csv(
    project: Project,
    path: str | Path | None = None,
    include_excluded: bool = False,
) -> Path:
    """Export the lightcurve of ``project`` as a CSV file and return its path.

    - ``path`` — the destination: None (default) writes
      ``<project>/lightcurve.csv`` (:data:`CSV_FILE`); an explicit string or
      :class:`~pathlib.Path` writes there instead (parent directories are
      created);
    - ``include_excluded`` — when False (default), points flagged
      ``excluded`` are OMITTED from the file (documented choice, see the
      module docstring); when True they are written too.

    The rows come from :func:`~.measurements.lightcurve` with the same
    ``include_excluded`` value, so the file order is by ``jd`` (ties by row
    id) across all sessions; since that function returns at most one row per
    frame (issue 13), the ``jd`` column never contains duplicate rows from a
    re-run of photometry. The header is exactly
    ``jd,utc,mag,mag_err,session,target``; ``jd``/``mag``/``mag_err`` are
    written as ``repr(float)`` (re-parseable with ``float()`` to the exact db
    value), ``mag_err`` NULL as an empty field, ``session`` as the session id,
    ``target`` as :func:`target_name` — the project config's target name,
    falling back to the saved target star pick's label when the config
    target is null (issue 18), empty string when neither is set.

    The file is written ATOMICALLY: temp file in the same directory + flush +
    fsync + :func:`os.replace`, temp removed on any failure incl.
    :class:`KeyboardInterrupt` — a crash never leaves a partial CSV and no
    ``*.tmp`` is left behind. Returns the written path.

    Raises :class:`TypeError` when ``path`` is neither a string, a
    :class:`~pathlib.Path`, nor None, or when ``include_excluded`` is not a
    bool; :class:`OSError` propagates when the write itself fails (the
    previous file, if any, stays intact).
    """
    if not isinstance(include_excluded, bool):
        raise TypeError(f"include_excluded must be a bool, got {include_excluded!r}")
    if path is None:
        dest = project.path / CSV_FILE
    else:
        if not isinstance(path, (str, Path)):
            raise TypeError(f"path must be a string, a Path, or None, got {path!r}")
        dest = Path(path)
    name = target_name(project)
    rows = lightcurve(project, include_excluded=include_excluded)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(CSV_HEADER)
            for measurement in rows:
                writer.writerow(_format_row(measurement, name))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
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
    return dest
