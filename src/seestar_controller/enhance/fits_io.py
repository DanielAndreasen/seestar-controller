"""FITS loading + project-stack listing — the PURE I/O layer of
:mod:`seestar_controller.enhance` (image-enhance ticket 01).

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) — stdlib +
:mod:`numpy` + :mod:`seestar_controller.store` only. The one heavy third-party
import, :mod:`astropy.io.fits`, is lazy inside :func:`load_fits` — the same
pattern as :mod:`seestar_controller.store.frames`. The widget layer
(:mod:`.viewer`) imports ``gi`` at module top and is never imported by this
module or eagerly by the package ``__init__``.

What lives here:

- :class:`EnhanceError` — the package's error type; every failure of this
  package (unreadable FITS, no project, unknown stack, bad render parameter)
  raises it;
- :class:`FitsImage` — one loaded FITS image: ``data`` as a float32 numpy
  array (``(H, W)`` grayscale or ``(H, W, 3)`` RGB, NaN for missing pixels),
  the primary header as a plain dict, and the source ``path``;
- :func:`load_fits` — read a FITS file with :mod:`astropy.io.fits`, applying
  ``BZERO``/``BSCALE``/``BLANK`` MANUALLY (the file is opened with
  ``do_not_scale_image_data=True`` so the raw storage values come through and
  the physical values are computed here — NaN-safe: non-finite pixels become
  NaN);
- :class:`StackInfo` — one stored **frame** of a project as listed by
  :func:`list_project_frames`: the store :class:`~seestar_controller.store.frames.Frame`
  fields (including the frame ``kind`` — ``"sub"`` / ``"stack"`` /
  ``"enhanced"``) plus the absolute file ``path`` (via
  :func:`~seestar_controller.store.frames.get_frame_path`) and the session's
  start time. A **file-based entry** (a stitched mosaic, see
  :func:`list_project_mosaics`) is also a :class:`StackInfo`, but with
  ``frame_id=None`` and ``kind="mosaic"`` — it has no ``frames`` row;
- :func:`list_project_frames` — all stored frames of a project across all
  sessions (``store.frames.list_frames(project)`` + the session records),
  ordered by ``jd``; store errors are wrapped into :class:`EnhanceError`.
  :func:`list_project_stacks` is the ``kind="stack"``-only view of the same
  listing (kept for the stack-only callers);
- :func:`list_project_mosaics` — the stitched mosaics of a project
  (``mosaics/<session_id>.fits``, mosaic-stitch ticket 01): FILE-BASED
  products, NOT ``frames`` rows, listed as :class:`StackInfo` with
  ``frame_id=None`` and ``kind="mosaic"`` (app-improvement ticket 08 — the
  Enhance view lists and opens them like any other image);
- :func:`stack_info_for_path` — a transient :class:`StackInfo` for an
  arbitrary FITS file opened by path (the viewer's open-file path).

Synthetic-FITS helpers for tests live in the tests, not here (this package
only reads).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from seestar_controller.mosaic import MOSAICS_DIR
from seestar_controller.store.errors import ProjectError
from seestar_controller.store.frames import (
    KIND_STACK,
    Frame,
    get_frame_path,
    header_to_dict,
    list_frames,
)
from seestar_controller.store.project import Project
from seestar_controller.store.sessions import list_sessions


class EnhanceError(Exception):
    """Any image-enhance failure: an unreadable/unsupported FITS file, no
    project set, an unknown stack id, a bad render parameter, a store error
    while listing stacks."""


#: Viewer-listing kind of a **stitched mosaic** — a FILE-BASED product of the
#: project (``mosaics/<session_id>.fits``, mosaic-stitch ticket 01) that is
#: NOT a ``frames`` row, so it has no frame id (app-improvement ticket 08:
#: the Enhance view lists and opens it like any other image).
KIND_MOSAIC = "mosaic"

#: Viewer-listing kind of an arbitrary FITS file opened by path (the open-file
#: path), when it is not one of the project's mosaics.
KIND_FILE = "file"

#: The Julian Date of the Unix epoch (1970-01-01T00:00:00 UTC).
_JD_UNIX_EPOCH = 2440587.5

#: Seconds per day (for the mtime -> JD conversion of file-based entries).
_SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True)
class FitsImage:
    """One loaded FITS image.

    - ``data`` — float32 numpy array, ``(H, W)`` grayscale or ``(H, W, 3)``
      RGB; missing/invalid pixels are NaN (see :func:`load_fits`);
    - ``header`` — the image HDU's header as a plain dict (astropy card
      values converted to plain Python scalars where astropy does so);
    - ``path`` — the file it was loaded from (None when built in memory).
    """

    data: np.ndarray
    header: dict[str, Any]
    path: Path | None = None

    @property
    def shape(self) -> tuple[int, ...]:
        """The data shape: ``(H, W)`` or ``(H, W, 3)``."""
        return self.data.shape

    @property
    def is_rgb(self) -> bool:
        """Whether the image is RGB (``(H, W, 3)``)."""
        return self.data.ndim == 3

    @property
    def height(self) -> int:
        return int(self.data.shape[0])

    @property
    def width(self) -> int:
        return int(self.data.shape[1])


@dataclass(frozen=True)
class StackInfo:
    """One listed **entry** of a project in the Enhance view: either a stored
    **frame** (as listed by :func:`list_project_frames` — any ``kind``: sub,
    stack, or enhanced) or a **file-based product** (a stitched mosaic, see
    :func:`list_project_mosaics`).

    - ``frame_id`` — the store frame row id (the id :meth:`ViewerController.open_stack`
      takes); **None** for a file-based entry (a mosaic), which has no
      ``frames`` row;
    - ``session_id`` — the session the entry belongs to (for a mosaic:
      the stitched session; for an arbitrary file: the file stem);
    - ``session_started_at`` — the session's start (UTC ISO), None when the
      session row is missing (hand-edited db);
    - ``path`` — the absolute path of the FITS file;
    - ``jd`` / ``utc`` — the observation time (Julian Date / UTC ISO-8601);
      for a file-based entry: the file's mtime;
    - ``target`` / ``exposure`` / ``gain`` — the frame metadata (None when
      unknown);
    - ``kind`` — the entry kind (``"sub"`` / ``"stack"`` / ``"enhanced"`` for
      a stored frame, :data:`KIND_MOSAIC` / :data:`KIND_FILE` for a
      file-based entry); defaults to ``"stack"`` so stack-only constructions
      keep working.
    """

    frame_id: int | None
    session_id: str
    session_started_at: str | None
    path: Path
    jd: float
    utc: str
    target: str | None
    exposure: float | None
    gain: float | None
    kind: str = KIND_STACK

    @property
    def key(self) -> str:
        """A unique list key for the viewer rows: ``frame:<id>`` for a stored
        frame, ``<kind>:<session_id>`` for a file-based entry."""
        if self.frame_id is not None:
            return f"frame:{self.frame_id}"
        return f"{self.kind}:{self.session_id}"

    def label(self) -> str:
        """The one-line list label: ``"2026-11-20T22:00:00.000+00:00 — M 42"``
        (a file-based entry: ``"… — mosaic <session_id>"``)."""
        if self.frame_id is None:
            return f"{self.utc} — {self.kind} {self.session_id}"
        return f"{self.utc} — {self.target or 'no target'}"

    def subtitle(self) -> str:
        """The second list line: the frame kind, session, jd, and
        exposure/gain when known."""
        parts = [self.kind, f"session {self.session_id}", f"jd {self.jd:.6f}"]
        if self.exposure is not None:
            parts.append(f"{self.exposure:g} s")
        if self.gain is not None:
            parts.append(f"gain {self.gain:g}")
        return " · ".join(parts)


def _header_float(header: dict[str, Any], card: str, default: float) -> float:
    """A header card as a finite float, or ``default`` when missing/invalid."""
    raw = header.get(card)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _to_float32_image(raw: np.ndarray, header: dict[str, Any]) -> np.ndarray:
    """Normalize a raw (unscaled) FITS data array to a float32 image.

    Applies ``BSCALE``/``BZERO`` manually (the file was opened with
    ``do_not_scale_image_data=True``), maps ``BLANK`` pixels of integer data
    to NaN, normalizes RGB axis order to ``(H, W, 3)``, and turns every
    non-finite value into NaN (NaN-safe: downstream only ever sees NaN).
    """
    arr = np.asarray(raw)
    data = arr.astype(np.float32)
    bscale = _header_float(header, "BSCALE", 1.0)
    bzero = _header_float(header, "BZERO", 0.0)
    if bscale != 1.0 or bzero != 0.0:
        data = data * np.float32(bscale) + np.float32(bzero)
    blank = header.get("BLANK")
    if blank is not None and arr.dtype.kind in "iu":
        try:
            data = np.where(arr == int(blank), np.float32(np.nan), data)
        except (TypeError, ValueError):
            pass  # a non-integer BLANK card: ignore
    if data.ndim == 2:
        pass
    elif data.ndim == 3:
        if data.shape[-1] in (3, 4):
            data = data[..., :3]  # (H, W, 3|4) -> RGB
        elif data.shape[0] in (3, 4):
            data = np.transpose(data[:3], (1, 2, 0))  # FITS (3|4, H, W) -> (H, W, 3)
        else:
            raise EnhanceError(
                f"unsupported FITS data shape {arr.shape} (expected 2-D or a "
                "3-channel image)"
            )
    else:
        raise EnhanceError(
            f"unsupported FITS data shape {arr.shape} (expected a 2-D image)"
        )
    # NaN-safe: every non-finite pixel (NaN, +-inf) becomes NaN
    data[~np.isfinite(data)] = np.nan
    return np.ascontiguousarray(data, dtype=np.float32)


def load_fits(path: str | Path) -> FitsImage:
    """Load the FITS file at ``path`` into a :class:`FitsImage`.

    The file is opened with ``do_not_scale_image_data=True`` and the physical
    values are computed here (``raw * BSCALE + BZERO``, ``BLANK`` -> NaN), so
    the scaling is explicit and NaN-safe. The first HDU with image data wins
    (usually the primary). RGB data is normalized to ``(H, W, 3)``.

    Raises :class:`EnhanceError` when the file is missing, not a readable
    FITS file, has no image data in any HDU, or has an unsupported data shape.
    """
    from astropy.io import fits as pyfits

    file_path = Path(path)
    try:
        with pyfits.open(file_path, memmap=False, do_not_scale_image_data=True) as hdul:
            hdu = next((h for h in hdul if getattr(h, "data", None) is not None), None)
            if hdu is None:
                raise EnhanceError(f"{file_path} has no image data in any HDU")
            raw = np.asarray(hdu.data)
            # sanitized plain dict: real Seestar FITS carry consecutive COMMENT
            # cards whose joined value contains "\n" (rejected by astropy when
            # re-inserted into a Header, e.g. by astropy.wcs.WCS)
            header = header_to_dict(hdu.header)
    except EnhanceError:
        raise
    except Exception as exc:
        raise EnhanceError(f"cannot read FITS file {file_path}: {exc}") from exc
    data = _to_float32_image(raw, header)
    return FitsImage(data=data, header=header, path=file_path)


def list_project_frames(
    project: Project | None, *, kind: str | None = None
) -> list[StackInfo]:
    """List the stored **frames** of ``project`` across all sessions, ordered
    by ``jd`` (the store's frame order).

    With ``kind=None`` (the default) ALL frames are listed — subs, stacks,
    and enhanced frames alike (sub-frames ticket 01: the image viewer lists
    and opens any stored frame); pass ``kind="stack"`` (or another
    :data:`~seestar_controller.store.frames.KINDS` member) to restrict.

    Uses :func:`seestar_controller.store.frames.list_frames` plus the session
    records (for the session start time) and
    :func:`seestar_controller.store.frames.get_frame_path` for the absolute
    file path. Raises :class:`EnhanceError` when ``project`` is None or a
    store error occurs (wrapped, with the cause chained).
    """
    if project is None:
        raise EnhanceError(
            "no project — set one with ViewerController.set_project() first "
            "(the Projects view is a later ticket)"
        )
    try:
        frames: list[Frame] = list_frames(project, kind=kind)
        sessions = {s.id: s for s in list_sessions(project)}
        infos: list[StackInfo] = []
        for frame in frames:
            path = get_frame_path(project, frame)
            session = sessions.get(frame.session_id)
            infos.append(
                StackInfo(
                    frame_id=frame.id,
                    session_id=frame.session_id,
                    session_started_at=session.started_at if session else None,
                    path=path,
                    jd=frame.jd,
                    utc=frame.utc,
                    target=frame.target,
                    exposure=frame.exposure,
                    gain=frame.gain,
                    kind=frame.kind,
                )
            )
    except ProjectError as exc:
        raise EnhanceError(
            f"cannot list the frames of project {project.slug!r}: {exc}"
        ) from exc
    return infos


def list_project_stacks(project: Project | None) -> list[StackInfo]:
    """List only the **Stacks** (GLOSSARY.md) of ``project`` — the
    ``kind="stack"`` view of :func:`list_project_frames` (kept for the
    stack-only callers)."""
    return list_project_frames(project, kind=KIND_STACK)


def _file_entry(
    path: Path,
    *,
    kind: str,
    session_id: str,
    session_started_at: str | None = None,
) -> StackInfo:
    """Build the file-based :class:`StackInfo` of the FITS file ``path``
    (``frame_id=None``; ``jd``/``utc`` from the file's mtime — the same
    fallback :func:`seestar_controller.store.frames.store_frame` uses when
    ``DATE-OBS`` is missing).

    Raises :class:`EnhanceError` when the file cannot be stat-ed.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise EnhanceError(f"cannot stat file {path}: {exc}") from exc
    utc = datetime.fromtimestamp(mtime, tz=UTC).isoformat(timespec="milliseconds")
    jd = _JD_UNIX_EPOCH + mtime / _SECONDS_PER_DAY
    return StackInfo(
        frame_id=None,
        session_id=session_id,
        session_started_at=session_started_at,
        path=path,
        jd=jd,
        utc=utc,
        target=None,
        exposure=None,
        gain=None,
        kind=kind,
    )


def list_project_mosaics(project: Project | None) -> list[StackInfo]:
    """List the **stitched mosaics** of ``project`` — the file-based products
    under ``mosaics/`` (``mosaics/<session_id>.fits``, mosaic-stitch ticket
    01; NOT ``frames`` rows), ordered by file mtime (app-improvement ticket
    08: the Enhance view lists and opens them like any other image).

    Each is a :class:`StackInfo` with ``frame_id=None``, ``kind``
    :data:`KIND_MOSAIC`, ``session_id`` = the mosaic file's stem (the stitched
    session), and ``jd``/``utc`` from the file's mtime. A missing
    ``mosaics/`` directory is an empty list, not an error.

    Raises :class:`EnhanceError` when ``project`` is None or a store error
    occurs while reading the sessions (wrapped, with the cause chained).
    """
    if project is None:
        raise EnhanceError(
            "no project — set one with ViewerController.set_project() first "
            "(the Projects view is a later ticket)"
        )
    mosaics_dir = project.path / MOSAICS_DIR
    if not mosaics_dir.is_dir():
        return []
    try:
        sessions = {s.id: s for s in list_sessions(project)}
    except ProjectError as exc:
        raise EnhanceError(
            f"cannot list the sessions of project {project.slug!r}: {exc}"
        ) from exc
    infos = [
        _file_entry(
            path,
            kind=KIND_MOSAIC,
            session_id=path.stem,
            session_started_at=(
                sessions[path.stem].started_at if path.stem in sessions else None
            ),
        )
        for path in sorted(mosaics_dir.glob("*.fits"))
        if path.is_file()
    ]
    infos.sort(key=lambda info: info.jd)
    return infos


def stack_info_for_path(
    path: str | Path, *, kind: str = KIND_FILE, session_id: str | None = None
) -> StackInfo:
    """A transient :class:`StackInfo` for an arbitrary FITS file opened by
    path (the viewer's open-file path — e.g. a freshly stitched mosaic before
    the list is refreshed). ``kind`` defaults to :data:`KIND_FILE`;
    ``session_id`` defaults to the file stem.

    Raises :class:`EnhanceError` when the file cannot be stat-ed.
    """
    file_path = Path(path)
    return _file_entry(
        file_path,
        kind=kind,
        session_id=session_id if session_id is not None else file_path.stem,
    )
