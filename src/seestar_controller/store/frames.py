"""Frame storage — sub-frames and stacks as FITS files + db rows in a project.

Pure-Python (module boundary per ``.scratch/project-store/spec.md``): no GTK
imports, no :mod:`seestar_controller.link` imports, stdlib only. The one
third-party import, :mod:`astropy` (FITS header parsing / Julian Date), is
lazy inside the functions that need it — the same pattern as
``link/mock_scope.py``, re-declared here because this package must not import
``link``.

A **frame** is one stored FITS file: a **sub-frame** (one individual exposure,
GLOSSARY.md), a **Stack** (the stacked FITS image), or an **enhanced** image
(the pipeline output saved by image-enhance ticket 04 — stored next to the
stacks, never over the original). Files live under the project directory::

    subs/<session_id>/<frame>.fits        kind "sub"
    stacks/<session_id>/<stack>.fits      kind "stack"
    enhanced/<session_id>/<frame>.fits    kind "enhanced"

and every file has one row in the ``frames`` table of the project db (see
:mod:`.schema`): the session FK, ``kind``, the project-relative ``path``, the
observation time as ``jd`` (Julian Date — the photometry standard) and ``utc``
(ISO-8601), plus ``exposure``/``gain``/``target`` metadata.

API (all take the open :class:`~.project.Project` handle first):

- :func:`store_frame` — write the FITS file atomically + insert the db row,
  returns the :class:`Frame`;
- :func:`list_frames` — frames with metadata, ordered by ``jd``;
- :func:`get_frame_path` / :func:`read_frame_bytes` — locate / read a frame's
  file.

Documented choices:

- **Ordering: file first, then db row.** The FITS bytes are written atomically
  (temp file in the SAME directory + flush + fsync + :func:`os.replace`, temp
  removed on any failure incl. :class:`KeyboardInterrupt` — the same pattern
  as :func:`.project.Project.save_config` and ``link/frames.py``, re-declared
  here because this package must not import them), then the ``frames`` row is
  inserted in one transaction (``with project.db:``). If the insert fails, the
  just-written file is removed again — a failure never leaves an orphan file
  (nor a db row without a file).
- **JD** comes from the FITS ``DATE-OBS`` header (parsed with
  :mod:`astropy.time`, ``Time.jd``); if ``DATE-OBS`` is missing or invalid, the
  written file's mtime (UTC) is used instead. ``utc`` is the ISO-8601 string
  (``+00:00``, millisecond precision — same format as
  :func:`.project.utc_now_iso`) of that same instant.
- **Frame file name** — ``<jd>.fits`` with the JD at fixed 6-decimal precision
  (``f"{jd:.6f}"``, ~86 ms resolution), e.g. ``2461365.416668.fits``; on a
  collision (two frames with the same JD to 6 decimals) a ``-2``, ``-3``, ...
  suffix is appended before ``.fits`` (same policy as session ids in
  :mod:`.sessions`).
- **Metadata precedence** — ``meta`` (the scope-sidecar mapping) overrides
  ``exposure``/``gain``/``target``; the FITS header cards (``EXPTIME`` /
  ``GAIN`` / ``OBJECT``) are the fallback. ``target`` is stored as TEXT: a
  plain string as-is, a mapping as its ``name`` entry (or its JSON encoding
  when it has no ``name``).
- **Unknown session** — :func:`store_frame` checks the session exists first
  and raises :class:`~.errors.SessionNotFoundError` (a clear error instead of
  the raw FK :class:`sqlite3.IntegrityError`), before anything is written.
  Frames may be added to a closed session (e.g. late downloads).
- Bytes that are not a readable FITS file at all raise
  :class:`~.errors.FrameStoreError` — garbage is never stored as ``.fits``.
"""

from __future__ import annotations

import io
import json
import math
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .errors import FrameStoreError, SessionNotFoundError
from .project import STACKS_DIR, SUBS_DIR, Project

#: Frame kind: a **sub-frame** (one individual exposure, GLOSSARY.md).
KIND_SUB = "sub"
#: Frame kind: a **Stack** (the stacked FITS image, GLOSSARY.md).
KIND_STACK = "stack"
#: Frame kind: an **enhanced** image (the saved pipeline output of a stack —
#: image-enhance ticket 04; the original stack file is never touched).
KIND_ENHANCED = "enhanced"
#: The valid frame kinds.
KINDS = (KIND_SUB, KIND_STACK, KIND_ENHANCED)

#: Directory for enhanced FITS files (``enhanced/<session>/`` per session) —
#: image-enhance ticket 04. Like the session subdirectories, it is created
#: lazily by :func:`store_frame`.
ENHANCED_DIR = "enhanced"

#: Frame kind -> its top-level directory inside the project (see
#: :data:`~.project.PROJECT_SUBDIRS`).
KIND_DIRS = {
    KIND_SUB: SUBS_DIR,
    KIND_STACK: STACKS_DIR,
    KIND_ENHANCED: ENHANCED_DIR,
}

#: Suffix of the stored FITS files.
FITS_SUFFIX = ".fits"

#: Decimals of the JD used in the file name (~86 ms resolution at 6 decimals).
JD_NAME_DECIMALS = 6

#: Upper bound for the collision suffix when deriving a frame file name.
_MAX_NAME_SUFFIX = 999

#: FITS header cards read as metadata / time fallback.
_HEADER_DATE_OBS = "DATE-OBS"
_HEADER_EXPOSURE = "EXPTIME"
_HEADER_GAIN = "GAIN"
_HEADER_TARGET = "OBJECT"


def normalize_kind(kind: Any) -> str:
    """Validate a frame ``kind`` ("sub", "stack", or "enhanced"); raise
    ``ValueError``."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    return str(kind)


@dataclass(frozen=True)
class Frame:
    """One stored FITS file as recorded in the ``frames`` table.

    - ``id`` — the db row id (INTEGER PRIMARY KEY);
    - ``session_id`` — the session FK (also the ``subs/<session>/`` dir name);
    - ``kind`` — ``"sub"``, ``"stack"``, or ``"enhanced"``;
    - ``path`` — the FITS file path RELATIVE to the project root, POSIX
      separators (e.g. ``subs/20261120T220000000000Z/2461365.416668.fits``);
    - ``jd`` — the observation time as Julian Date;
    - ``utc`` — the same instant as a UTC ISO-8601 string (``+00:00``);
    - ``exposure`` / ``gain`` / ``target`` — metadata (None when unknown).
    """

    id: int
    session_id: str
    kind: str
    path: str
    jd: float
    utc: str
    exposure: float | None
    gain: float | None
    target: str | None


def _row_to_frame(row: sqlite3.Row | tuple[Any, ...]) -> Frame:
    return Frame(
        id=int(row[0]),
        session_id=str(row[1]),
        kind=str(row[2]),
        path=str(row[3]),
        jd=float(row[4]),
        utc=str(row[5]),
        exposure=None if row[6] is None else float(row[6]),
        gain=None if row[7] is None else float(row[7]),
        target=None if row[8] is None else str(row[8]),
    )


def _read_fits_header(fits_bytes: bytes) -> dict[str, Any]:
    """Read the primary HDU header of ``fits_bytes`` (lazy astropy import).

    Raises :class:`FrameStoreError` when the bytes are not a readable FITS
    file.
    """
    from astropy.io import fits as pyfits

    try:
        with pyfits.open(io.BytesIO(fits_bytes), memmap=False) as hdul:
            return dict(hdul[0].header)
    except Exception as exc:
        raise FrameStoreError(f"not a readable FITS file: {exc}") from exc


def _instant_to_jd(instant: datetime) -> float:
    """The Julian Date of an aware UTC ``instant`` (astropy ``Time.jd``)."""
    from astropy.time import Time

    return float(Time(instant, scale="utc").jd)


def _utc_iso(instant: datetime) -> str:
    """Format ``instant`` as a UTC ISO-8601 string with explicit ``+00:00``
    offset and millisecond precision (same format as
    :func:`.project.utc_now_iso`)."""
    return instant.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _date_obs_jd_utc(raw: Any) -> tuple[float, str] | None:
    """Parse a FITS ``DATE-OBS`` value to a ``(jd, utc)`` pair, or None when
    missing/invalid (the caller then falls back to the file mtime).

    Tries astropy's ``isot`` and ``fits`` formats, then the stdlib ISO parser
    (``Z`` suffix allowed); the JD is ``astropy.time.Time.jd``.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    from astropy.time import Time

    for fmt in ("isot", "fits"):
        try:
            t = Time(text, format=fmt, scale="utc")
        except Exception:
            continue
        jd = float(t.jd)
        if math.isfinite(jd):
            return jd, _utc_iso(t.to_datetime(timezone.utc))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)  # naive -> UTC
    instant = parsed.astimezone(timezone.utc)
    return _instant_to_jd(instant), _utc_iso(instant)


def _optional_float(value: Any, *, what: str) -> float | None:
    """Coerce a caller-supplied metadata value to a finite float (None stays
    None); ``ValueError`` on anything else."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{what} must be a number, got {value!r}")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number, got {value!r}") from None
    if not math.isfinite(result):
        raise ValueError(f"{what} must be finite, got {value!r}")
    return result


def _header_float(header: Mapping[str, Any], card: str) -> float | None:
    """A FITS header card as a finite float, or None when missing/invalid."""
    raw = header.get(card)
    if raw is None:
        return None
    try:
        result = float(raw)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _coerce_target(value: Any) -> str | None:
    """Normalize a ``target`` metadata value to the TEXT stored in the db.

    None -> None; a string is stored as-is; a mapping (the scope sidecar's
    ``{"ra": ..., "dec": ..., "name": ...}``) is stored as its ``name`` entry
    when present, else as its JSON encoding; other scalars as ``str(value)``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        name = value.get("name")
        if name is not None:
            return str(name)
        return json.dumps(dict(value), sort_keys=True, default=str)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    raise TypeError(f"target must be a string, a mapping, or None, got {value!r}")


def _resolve_meta(
    meta: Mapping[str, Any] | None, header: Mapping[str, Any]
) -> tuple[float | None, float | None, str | None]:
    """Resolve ``(exposure, gain, target)``: ``meta`` (the scope sidecar)
    overrides, the FITS header cards are the fallback (a None meta value
    counts as absent)."""
    meta = meta or {}
    exposure = _optional_float(meta.get("exposure"), what="meta exposure")
    if exposure is None:
        exposure = _header_float(header, _HEADER_EXPOSURE)
    gain = _optional_float(meta.get("gain"), what="meta gain")
    if gain is None:
        gain = _header_float(header, _HEADER_GAIN)
    target = meta.get("target")
    if target is None:
        target = header.get(_HEADER_TARGET)
    return exposure, gain, _coerce_target(target)


def _frame_file_name(base_dir: Path, jd: float) -> str:
    """The file name for a frame with Julian Date ``jd`` in ``base_dir``:
    ``<jd:.6f>.fits`` with a ``-2``/``-3``/... collision suffix."""
    stem = f"{jd:.{JD_NAME_DECIMALS}f}"
    name = stem + FITS_SUFFIX
    suffix = 2
    while (base_dir / name).exists():
        if suffix > _MAX_NAME_SUFFIX:
            raise FrameStoreError(
                f"cannot derive a unique frame file name from jd {jd!r} in "
                f"{base_dir} (tried suffixes up to -{_MAX_NAME_SUFFIX})"
            )
        name = f"{stem}-{suffix}{FITS_SUFFIX}"
        suffix += 1
    return name


def _remove_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def store_frame(
    project: Project,
    session_id: str,
    kind: str,
    fits_bytes: bytes,
    meta: Mapping[str, Any] | None = None,
) -> Frame:
    """Store one FITS frame in ``project`` and return its :class:`Frame`.

    - ``session_id`` — the session the frame belongs to; must exist, else
      :class:`~.errors.SessionNotFoundError` is raised before anything is
      written;
    - ``kind`` — ``"sub"``, ``"stack"``, or ``"enhanced"`` (else
      ``ValueError``); the file goes to ``subs/<session_id>/``,
      ``stacks/<session_id>/``, or ``enhanced/<session_id>/`` (created
      lazily, see :data:`KIND_DIRS`);
    - ``fits_bytes`` — the raw FITS file content; must be a readable FITS
      file, else :class:`~.errors.FrameStoreError`;
    - ``meta`` — optional mapping (the scope sidecar) whose ``exposure`` /
      ``gain`` / ``target`` override the FITS header values.

    The file is written FIRST, atomically (temp file in the same directory +
    fsync + :func:`os.replace`, temp removed on any failure incl.
    :class:`KeyboardInterrupt`), then the ``frames`` row is inserted in one
    transaction; if the insert fails the file is removed again (no orphan
    files). The observation time (``jd``/``utc``) comes from the FITS
    ``DATE-OBS`` header, falling back to the written file's mtime (UTC). The
    file name is derived from the JD (see the module docstring).
    """
    kind = normalize_kind(kind)
    if not isinstance(session_id, str):
        raise TypeError(f"session_id must be a string, got {session_id!r}")
    if isinstance(fits_bytes, (bytearray, memoryview)):
        fits_bytes = bytes(fits_bytes)
    if not isinstance(fits_bytes, bytes):
        raise TypeError(f"fits_bytes must be bytes, got {type(fits_bytes).__name__}")
    if meta is not None and not isinstance(meta, Mapping):
        raise TypeError(f"meta must be a mapping or None, got {meta!r}")

    # unknown session -> clear error BEFORE anything is written
    row = project.db.execute(
        "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise SessionNotFoundError(
            f"project {project.slug!r} has no session {session_id!r}"
        )

    header = _read_fits_header(fits_bytes)
    exposure, gain, target = _resolve_meta(meta, header)

    kind_dir = KIND_DIRS[kind]
    base_dir = project.path / kind_dir / session_id
    base_dir.mkdir(parents=True, exist_ok=True)

    # phase 1: write the bytes to a temp file in the SAME directory (same
    # filesystem, so os.replace is atomic), fsync, then rename to the final
    # name derived from the JD
    fd, tmp_name = tempfile.mkstemp(dir=base_dir, prefix=".frame-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(fits_bytes)
            fh.flush()
            os.fsync(fh.fileno())
        jd_utc = _date_obs_jd_utc(header.get(_HEADER_DATE_OBS))
        if jd_utc is None:
            # no/invalid DATE-OBS -> the file's mtime (UTC); os.replace
            # preserves the temp file's mtime, so it IS the final file's mtime
            instant = datetime.fromtimestamp(os.stat(tmp).st_mtime, timezone.utc)
            jd_utc = (_instant_to_jd(instant), _utc_iso(instant))
        jd, utc = jd_utc
        if not math.isfinite(jd):
            raise FrameStoreError(
                f"derived a non-finite JD from DATE-OBS "
                f"{header.get(_HEADER_DATE_OBS)!r}"
            )
        name = _frame_file_name(base_dir, jd)
        dest = base_dir / name
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.close(fd)  # already closed when the with-block exited
        except OSError:
            pass
        _remove_quietly(tmp)
        raise
    rel_path = (PurePosixPath(kind_dir) / session_id / name).as_posix()

    # phase 2: the db row, in one transaction; on failure remove the file again
    try:
        with project.db:
            cur = project.db.execute(
                "INSERT INTO frames (session_id, kind, path, jd, utc, exposure, gain, target) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, kind, rel_path, jd, utc, exposure, gain, target),
            )
            frame_id = int(cur.lastrowid)
    except (KeyboardInterrupt, SystemExit):
        _remove_quietly(dest)
        raise
    except Exception as exc:
        _remove_quietly(dest)
        raise FrameStoreError(
            f"stored the file {rel_path!r} but could not insert its frames row: {exc}"
        ) from exc
    return Frame(
        id=frame_id,
        session_id=session_id,
        kind=kind,
        path=rel_path,
        jd=jd,
        utc=utc,
        exposure=exposure,
        gain=gain,
        target=target,
    )


def list_frames(
    project: Project,
    session_id: str | None = None,
    kind: str | None = None,
) -> list[Frame]:
    """List frames of ``project`` as :class:`Frame` records, ordered by ``jd``
    (ties broken by ``id``).

    - ``session_id`` — restrict to one session; an unknown id simply yields an
      empty list (this is a filter, not a lookup);
    - ``kind`` — restrict to ``"sub"`` or ``"stack"`` (else ``ValueError``).
    """
    query = "SELECT id, session_id, kind, path, jd, utc, exposure, gain, target FROM frames"
    clauses: list[str] = []
    params: list[Any] = []
    if session_id is not None:
        if not isinstance(session_id, str):
            raise TypeError(f"session_id must be a string or None, got {session_id!r}")
        clauses.append("session_id = ?")
        params.append(session_id)
    if kind is not None:
        clauses.append("kind = ?")
        params.append(normalize_kind(kind))
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY jd, id"
    rows = project.db.execute(query, params).fetchall()
    return [_row_to_frame(row) for row in rows]


def get_frame_path(project: Project, frame: Frame) -> Path:
    """The absolute path of ``frame``'s FITS file inside ``project``.

    Raises :class:`FrameStoreError` when the stored relative path would escape
    the project directory (hand-edited db row).
    """
    rel = PurePosixPath(frame.path)
    if rel.is_absolute() or ".." in rel.parts:
        raise FrameStoreError(
            f"frame {frame.id} has an unsafe path {frame.path!r} "
            "(escapes the project directory)"
        )
    return project.path.joinpath(*rel.parts)


def read_frame_bytes(project: Project, frame: Frame) -> bytes:
    """Read and return the FITS bytes of ``frame``.

    Raises :class:`FrameStoreError` when the file is missing or unreadable.
    """
    path = get_frame_path(project, frame)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise FrameStoreError(f"cannot read frame file {path}: {exc}") from exc
