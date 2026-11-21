"""Stack one session's subs into a kind='stack' frame — the project
integration of :mod:`seestar_controller.stacking` (sub-frames ticket 03).

Pure module: **no ``gi`` import** — stdlib + :mod:`numpy` +
:mod:`seestar_controller.store` only; the heavy :mod:`astropy.io.fits`
import stays lazy inside :func:`stack_session_subs` (the same pattern as
:mod:`seestar_controller.mosaic.session`).

- :func:`stack_session_subs` — collect the session's sub-frames
  (``store.frames`` ``kind="sub"``), median-combine them
  (:func:`~.core.median_combine` — scope subs are already aligned, so NO
  registration; documented assumption) and write the result back into the
  project as a ``kind="stack"`` FITS via
  :func:`~seestar_controller.store.frames.store_frame`, returning the new
  :class:`~seestar_controller.store.frames.Frame`. Because the product is a
  regular stack frame, ALL stack-based views work on it unchanged (the
  Enhance viewer's :func:`~seestar_controller.enhance.fits_io.list_project_stacks`,
  mosaic, report, crossmatch, ...).
- the records: :class:`SkippedSub`, :class:`StackSubsResult`.

Documented choices:

- **Per-frame failures are skipped, never crash**: a sub whose file is
  missing/unreadable, is not a parseable FITS, has no image data, or has a
  different shape than the first readable sub is recorded in
  ``StackSubsResult.skipped`` and left out of the combine; only when NO
  sub is readable does the call raise :class:`~.errors.StackSubsError`.
- **No subs at all** raises :class:`~.errors.StackSubsError`; an **unknown
  session** raises
  :class:`~seestar_controller.store.errors.SessionNotFoundError` (a lookup,
  not a filter — the same pattern as
  :func:`seestar_controller.mosaic.session.stitch_session`).
- **Header**: the first readable sub's cards (minus the structural cards
  the FITS writer manages itself), with ``DATE-OBS`` set to the MEDIAN of
  the combined subs' JDs (the stack represents the middle of the session)
  and ``HISTORY`` cards recording the combine; ``EXPTIME``/``GAIN``/
  ``OBJECT`` are inherited from the reference sub (the per-sub exposure,
  not summed).
- **Re-running appends**: every call writes a NEW stack frame (the store's
  ``store_frame`` semantics) — stacking twice yields two stack frames.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np

from seestar_controller.store.errors import ProjectError, SessionNotFoundError
from seestar_controller.store.frames import (
    KIND_STACK,
    KIND_SUB,
    Frame,
    header_to_dict,
    list_frames,
    read_frame_bytes,
    store_frame,
)
from seestar_controller.store.project import Project
from seestar_controller.store.sessions import list_sessions

from .core import median_combine
from .errors import StackSubsError

logger = logging.getLogger(__name__)

#: JD of the Unix epoch (1970-01-01T00:00:00 UTC) — for JD <-> ISO conversion.
_JD_UNIX_EPOCH = 2440587.5

#: FITS cards managed by the writer itself (or free-text) — dropped from the
#: reference sub's header when writing the stack.
_DROP_CARDS = frozenset(
    {
        "SIMPLE",
        "BITPIX",
        "NAXIS",
        "NAXIS1",
        "NAXIS2",
        "NAXIS3",
        "NAXIS4",
        "EXTEND",
        "CHECKSUM",
        "DATASUM",
        "COMMENT",
        "HISTORY",
    }
)


@dataclass(frozen=True)
class SkippedSub:
    """One sub-frame left out of the combine: ``frame_id`` + the
    human-readable ``reason``."""

    frame_id: int
    reason: str


@dataclass(frozen=True)
class StackSubsResult:
    """The result of :func:`stack_session_subs`.

    - ``frame`` — the new ``kind="stack"`` :class:`~seestar_controller.store.frames.Frame`;
    - ``n_subs`` — the number of subs combined into it;
    - ``skipped`` — the :class:`SkippedSub`\\ s (unreadable / mismatched).
    """

    frame: Frame
    n_subs: int
    skipped: tuple[SkippedSub, ...]


def _jd_to_date_obs(jd: float) -> str:
    """A JD (UTC) as a naive ISOT ``DATE-OBS`` string (milliseconds)."""
    unix = (jd - _JD_UNIX_EPOCH) * 86400.0
    instant = datetime.fromtimestamp(unix, tz=UTC)
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "")


def stack_session_subs(project: Any, session_id: Any) -> StackSubsResult:
    """Median-combine the sub-frames of ``session_id`` in ``project`` into
    one ``kind="stack"`` FITS written back into the project (via
    :func:`~seestar_controller.store.frames.store_frame`), and return the
    :class:`StackSubsResult` with the new frame.

    Scope subs of one session are already aligned by the telescope, so the
    combine is a plain pixel-wise median — NO registration (documented
    assumption). Per-frame failures are skipped (recorded in
    ``skipped``), never crash.

    Raises :class:`TypeError` for a non-Project handle or a non-str
    ``session_id``, :class:`~seestar_controller.store.errors.SessionNotFoundError`
    for an unknown session, and :class:`~.errors.StackSubsError` when the
    session has no subs or no readable sub.
    """
    if not isinstance(project, Project):
        raise TypeError(
            "project must be an open seestar_controller.store Project handle "
            f"(got {type(project).__name__})"
        )
    if not isinstance(session_id, str):
        raise TypeError(f"session_id must be a string, got {session_id!r}")
    if not any(s.id == session_id for s in list_sessions(project)):
        raise SessionNotFoundError(
            f"project {project.slug!r} has no session {session_id!r}"
        )
    subs = list_frames(project, session_id=session_id, kind=KIND_SUB)
    if not subs:
        raise StackSubsError(
            f"session {session_id!r} of project {project.slug!r} has no subs "
            "to stack"
        )

    from astropy.io import fits as pyfits  # lazy: heavy third-party import

    datas: list[np.ndarray] = []
    headers: list[dict[str, Any]] = []
    used: list[Frame] = []
    skipped: list[SkippedSub] = []
    for frame in subs:
        try:
            raw = read_frame_bytes(project, frame)
        except ProjectError as exc:
            logger.info("skipping sub %s: %s", frame.id, exc)
            skipped.append(SkippedSub(frame_id=frame.id, reason=str(exc)))
            continue
        try:
            with pyfits.open(io.BytesIO(raw), memmap=False) as hdul:
                data = hdul[0].data
                header = header_to_dict(hdul[0].header)
        except Exception as exc:  # noqa: BLE001 - any parse failure skips the sub
            logger.info("skipping sub %s: %s", frame.id, exc)
            skipped.append(
                SkippedSub(frame_id=frame.id, reason=f"not a readable FITS file: {exc}")
            )
            continue
        if data is None:
            skipped.append(SkippedSub(frame_id=frame.id, reason="no image data"))
            continue
        arr = np.asarray(data)
        if datas and arr.shape != datas[0].shape:
            skipped.append(
                SkippedSub(
                    frame_id=frame.id,
                    reason=f"shape {arr.shape} does not match {datas[0].shape}",
                )
            )
            continue
        datas.append(arr)
        headers.append(header)
        used.append(frame)
    if not datas:
        raise StackSubsError(
            f"no readable sub of session {session_id!r} in project "
            f"{project.slug!r} — nothing to stack"
        )

    combined = median_combine(datas)
    cards = {k: v for k, v in headers[0].items() if k not in _DROP_CARDS}
    median_jd = float(np.median([f.jd for f in used]))
    cards["DATE-OBS"] = _jd_to_date_obs(median_jd)
    hdu = pyfits.PrimaryHDU(data=combined, header=pyfits.Header(cards))
    hdu.header["HISTORY"] = (
        f"median-combined {len(datas)} subs of session {session_id} "
        f"(project {project.slug})"
    )
    if skipped:
        hdu.header["HISTORY"] = f"skipped {len(skipped)} unreadable/mismatched subs"
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)

    frame = store_frame(project, session_id, KIND_STACK, buf.getvalue())
    return StackSubsResult(frame=frame, n_subs=len(datas), skipped=tuple(skipped))
