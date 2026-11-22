"""Stack one session's subs into a kind='stack' frame — the project
integration of :mod:`seestar_controller.stacking` (sub-frames ticket 03).

Pure module: **no ``gi`` import** — stdlib + :mod:`numpy` +
:mod:`seestar_controller.store` only; the heavy :mod:`astropy.io.fits`
import stays lazy inside :func:`stack_session_subs` (the same pattern as
:mod:`seestar_controller.mosaic.session`).

- :func:`stack_session_subs` — collect the session's sub-frames
  (``store.frames`` ``kind="sub"``), combine them
  (:func:`~.core.median_combine`) and write the result back into the
  project as a ``kind="stack"`` FITS via
  :func:`~seestar_controller.store.frames.store_frame`, returning the new
  :class:`~seestar_controller.store.frames.Frame`. Because the product is a
  regular stack frame, ALL stack-based views work on it unchanged (the
  Enhance viewer's :func:`~seestar_controller.enhance.fits_io.list_project_stacks`,
  mosaic, report, crossmatch, ...).
- field rotation (stacking issue 01): on an alt-az mount the field rotates
  during a session, so a pixel-wise median of unregistered subs smears
  stars into arcs. When the subs carry a WCS — the solved
  ``frame_wcs`` record (preferred) or the sub's own header ``CD`` matrix —
  :func:`stack_session_subs` estimates each sub's field rotation
  (:func:`~.core.header_rotation_deg`), and when the total rotation over
  the session moves the farthest image corner by more than
  :data:`~.core.DEROTATE_THRESHOLD_PX` (~1 star FWHM), the subs are
  DEROTATED to a common reference rotation
  (:func:`~.core.derotate_array`) before the median. Without any WCS the
  combine stays a plain pixel-wise median (documented fallback assumption).
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
- **Derotation needs a rotation estimate for EVERY combined sub**: the
  solved ``frame_wcs`` record wins over the sub's header ``CD`` matrix; if
  even one combined sub has neither, no derotation happens and the result
  carries a ``rotation_warning`` instead (solve all subs to enable
  derotation). Subs without a WCS are still stacked — the plain median
  fallback.
- **Header**: the first readable sub's cards (minus the structural cards
  the FITS writer manages itself), with ``DATE-OBS`` set to the MEDIAN of
  the combined subs' JDs (the stack represents the middle of the session)
  and ``HISTORY`` cards recording the combine (and the derotation, when
  applied); ``EXPTIME``/``GAIN``/``OBJECT`` are inherited from the
  reference sub (the per-sub exposure, not summed).
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
from seestar_controller.store.wcs import load_frame_wcs

from .core import (
    DEROTATE_THRESHOLD_PX,
    derotate_array,
    header_rotation_deg,
    max_rotation_displacement_px,
    median_combine,
    rotation_exceeds_threshold,
    total_field_rotation_deg,
    wrap_180,
)
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
    - ``skipped`` — the :class:`SkippedSub`\\ s (unreadable / mismatched);
    - ``field_rotation_deg`` — the total field rotation over the combined
      subs in degrees (``[0, 180]``, from the per-sub WCS/header ``CD``
      matrices), or None when no sub carried a rotation estimate at all;
    - ``rotation_displacement_px`` — how far that rotation moves the
      farthest image corner, in pixels (None together with
      ``field_rotation_deg``);
    - ``derotated`` — True when the subs were derotated to a common
      reference rotation before the median (rotation known for every sub
      AND exceeding :data:`~.core.DEROTATE_THRESHOLD_PX`);
    - ``rotation_source`` — where the rotation estimates came from:
      ``"frame_wcs"`` (solved ``frame_wcs`` records), ``"header"`` (the
      subs' own FITS ``CD`` matrices), or None;
    - ``max_derotation_deg`` — the largest per-sub derotation applied
      (0.0 when not derotated).
    """

    frame: Frame
    n_subs: int
    skipped: tuple[SkippedSub, ...]
    field_rotation_deg: float | None = None
    rotation_displacement_px: float | None = None
    derotated: bool = False
    rotation_source: str | None = None
    max_derotation_deg: float = 0.0

    @property
    def rotation_warning(self) -> str | None:
        """A human-readable warning when the session's field rotation is
        large enough to blur a pixel-wise median but the subs could NOT be
        derotated (no WCS for at least one combined sub) — None otherwise.
        The UI appends this to the Stack-subs status line, together with the
        hint to plate-solve the subs (or stack a shorter subset)."""
        if (
            self.field_rotation_deg is not None
            and self.rotation_displacement_px is not None
            and not self.derotated
            and self.rotation_displacement_px >= DEROTATE_THRESHOLD_PX
        ):
            return (
                f"field rotation {self.field_rotation_deg:.1f}° over the session "
                f"(~{self.rotation_displacement_px:.1f} px at the corner, above the "
                f"~{DEROTATE_THRESHOLD_PX:g} px / 1 FWHM threshold) — the stack may be "
                "blurry; plate-solve the subs (WCS) to enable derotation, or stack "
                "a shorter subset of the session"
            )
        return None


def _jd_to_date_obs(jd: float) -> str:
    """A JD (UTC) as a naive ISOT ``DATE-OBS`` string (milliseconds)."""
    unix = (jd - _JD_UNIX_EPOCH) * 86400.0
    instant = datetime.fromtimestamp(unix, tz=UTC)
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "")


def _sub_rotation(project: Project, frame: Frame, header: dict[str, Any]) -> tuple[float | None, str | None]:
    """One sub's field rotation estimate ``(rotation_deg, source)``.

    The solved ``frame_wcs`` record (astrometry) wins — its cards carry the
    full solved ``CD`` matrix; otherwise the sub's own FITS header ``CD``
    matrix is used. ``(None, None)`` when neither yields a rotation (no
    WCS at all — the plain-median fallback).
    """
    try:
        wcs = load_frame_wcs(project, frame.id)
    except ProjectError as exc:  # a broken WCS record must not kill the stack
        logger.info("frame %s has an unreadable WCS record: %s", frame.id, exc)
        wcs = None
    if wcs is not None:
        rotation = header_rotation_deg(wcs.cards)
        if rotation is not None:
            return rotation, "frame_wcs"
    rotation = header_rotation_deg(header)
    if rotation is not None:
        return rotation, "header"
    return None, None


def stack_session_subs(project: Any, session_id: Any) -> StackSubsResult:
    """Median-combine the sub-frames of ``session_id`` in ``project`` into
    one ``kind="stack"`` FITS written back into the project (via
    :func:`~seestar_controller.store.frames.store_frame`), and return the
    :class:`StackSubsResult` with the new frame.

    Field rotation (stacking issue 01): each sub's rotation is estimated
    from its solved ``frame_wcs`` record (preferred) or its header ``CD``
    matrix; when the total rotation over the session exceeds
    :data:`~.core.DEROTATE_THRESHOLD_PX` at the farthest image corner AND
    every combined sub has a rotation estimate, the subs are derotated to a
    common reference rotation (the unwrapped median rotation) before the
    median. Otherwise the combine is a plain pixel-wise median — the
    documented fallback when no WCS is available — and the result's
    ``rotation_warning`` says so when the rotation is significant.
    Per-frame failures are skipped (recorded in ``skipped``), never crash.

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

    # --- field rotation (stacking issue 01): estimate per-sub rotations ---
    rotations: list[float | None] = []
    sources: list[str | None] = []
    for frame, header in zip(used, headers):
        rotation, source = _sub_rotation(project, frame, header)
        rotations.append(rotation)
        sources.append(source)
    known = [float(r) for r in rotations if r is not None]
    rotation_source = (
        "frame_wcs" if "frame_wcs" in sources else ("header" if known else None)
    )
    field_rotation = total_field_rotation_deg(known) if len(known) >= 2 else None
    shape2d = datas[0].shape[:2]
    displacement_px = (
        max_rotation_displacement_px(field_rotation, shape2d)
        if field_rotation is not None
        else None
    )
    # derotate only when EVERY combined sub has a rotation estimate and the
    # total rotation would visibly smear a pixel-wise median
    derotate = (
        field_rotation is not None
        and len(known) == len(datas)
        and rotation_exceeds_threshold(field_rotation, shape2d)
    )
    max_derotation = 0.0
    if derotate:
        rots = known  # every rotation is known here: len(known) == len(datas)
        base = rots[0]
        ref_rotation = base + float(np.median([wrap_180(r - base) for r in rots]))
        offsets = [wrap_180(ref_rotation - r) for r in rots]
        max_derotation = max(abs(o) for o in offsets)
        # derotate_array rotates by the pixel PA offset ref_rotation - sub_rotation
        datas = [derotate_array(arr, offset) for arr, offset in zip(datas, offsets)]
        logger.info(
            "stacking session %s: derotated %d subs to a common field rotation "
            "(total session rotation %.2f deg, max per-sub derotation %.2f deg)",
            session_id,
            len(datas),
            field_rotation,
            max_derotation,
        )

    combined = median_combine(datas, nan_safe=derotate)
    cards = {k: v for k, v in headers[0].items() if k not in _DROP_CARDS}
    median_jd = float(np.median([f.jd for f in used]))
    cards["DATE-OBS"] = _jd_to_date_obs(median_jd)
    hdu = pyfits.PrimaryHDU(data=combined, header=pyfits.Header(cards))
    hdu.header["HISTORY"] = (
        f"median-combined {len(datas)} subs of session {session_id} "
        f"(project {project.slug})"
    )
    if field_rotation is not None:
        hdu.header["HISTORY"] = (
            f"field rotation over the session: {field_rotation:.2f} deg "
            f"(source: {rotation_source}, ~{displacement_px:.1f} px at the corner)"
        )
    if derotate:
        hdu.header["HISTORY"] = (
            f"derotated {len(datas)} subs to a common field rotation before the "
            f"median (max per-sub derotation {max_derotation:.2f} deg)"
        )
    if skipped:
        hdu.header["HISTORY"] = f"skipped {len(skipped)} unreadable/mismatched subs"
    buf = io.BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)

    frame = store_frame(project, session_id, KIND_STACK, buf.getvalue())
    return StackSubsResult(
        frame=frame,
        n_subs=len(datas),
        skipped=tuple(skipped),
        field_rotation_deg=field_rotation,
        rotation_displacement_px=displacement_px,
        derotated=derotate,
        rotation_source=rotation_source,
        max_derotation_deg=max_derotation,
    )
