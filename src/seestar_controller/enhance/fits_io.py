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
  start time;
- :func:`list_project_frames` — all stored frames of a project across all
  sessions (``store.frames.list_frames(project)`` + the session records),
  ordered by ``jd``; store errors are wrapped into :class:`EnhanceError`.
  :func:`list_project_stacks` is the ``kind="stack"``-only view of the same
  listing (kept for the stack-only callers).

Synthetic-FITS helpers for tests live in the tests, not here (this package
only reads).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

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
    """One stored **frame** of a project, as listed by
    :func:`list_project_frames` (any ``kind`` — sub, stack, or enhanced;
    sub-frames ticket 01: the viewer lists and opens all of them).

    - ``frame_id`` — the store frame row id (the id :meth:`ViewerController.open_stack`
      takes);
    - ``session_id`` — the session the frame belongs to;
    - ``session_started_at`` — the session's start (UTC ISO), None when the
      session row is missing (hand-edited db);
    - ``path`` — the absolute path of the FITS file;
    - ``jd`` / ``utc`` — the observation time (Julian Date / UTC ISO-8601);
    - ``target`` / ``exposure`` / ``gain`` — the frame metadata (None when
      unknown);
    - ``kind`` — the frame kind (``"sub"`` / ``"stack"`` / ``"enhanced"``);
      defaults to ``"stack"`` so stack-only constructions keep working.
    """

    frame_id: int
    session_id: str
    session_started_at: str | None
    path: Path
    jd: float
    utc: str
    target: str | None
    exposure: float | None
    gain: float | None
    kind: str = KIND_STACK

    def label(self) -> str:
        """The one-line list label: ``"2026-11-20T22:00:00.000+00:00 — M 42"``."""
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
