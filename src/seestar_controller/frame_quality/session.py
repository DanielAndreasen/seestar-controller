"""Session scoring — score all sub-frames of one session and flag outliers.

Pure module of :mod:`seestar_controller.frame_quality` (frame-quality ticket
01): **no ``gi`` import** — stdlib + :mod:`numpy` +
:mod:`seestar_controller.store` + :mod:`.metrics` only (the heavy
astropy/photutils imports stay lazy inside :func:`.metrics.score_frame`).

- :func:`score_session` — score ALL **sub-frames** of one session of a
  project (``store.frames`` ``kind="sub"`` only — stacks and enhanced images
  are NEVER scored), flag the outliers per session with the robust MAD
  z-score (:func:`.metrics.flag_outliers`, threshold 3.0 by default),
  persist every metrics + flag record via
  :func:`seestar_controller.store.quality.save_frame_quality`, and return a
  :class:`SessionScore`;
- :class:`ScoredFrame` / :class:`SkippedFrame` / :class:`SessionScore` — the
  frozen result records.

Documented choices:

- **Sub-frames only.** Like :func:`seestar_controller.photometry.measure.measure_project`,
  only ``kind="sub"`` frames are scored — a stack or an enhanced image is
  not a sub-frame.
- **Unknown session** raises :class:`~seestar_controller.store.errors.SessionNotFoundError`
  (a lookup, not a filter). A session with no sub-frames yields an empty
  :class:`SessionScore` (nothing scored, nothing persisted).
- **Unreadable/unscoreable frames are skipped, never a crash** (same pattern
  as the photometry run): a frame whose file cannot be read or parsed, or
  whose scoring raises :class:`.metrics.QualityError`, is recorded as a
  :class:`SkippedFrame` with the reason.
- **Flagging is per session**: the robust z-scores are computed over the
  session's scored frames only (never across sessions), per metric, two-sided
  — see :func:`.metrics.flag_outliers`.
- **Persistence** (``persist=True``, the default): every scored frame's
  metrics + flag + reasons are upserted into the project db via
  :func:`seestar_controller.store.quality.save_frame_quality` (one
  transaction per frame), so the query API
  :func:`seestar_controller.store.quality.session_quality` returns them
  afterwards. ``persist=False` scores in memory only.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from typing import Any

from seestar_controller.store.errors import ProjectError, SessionNotFoundError
from seestar_controller.store.frames import (
    KIND_SUB,
    Frame,
    list_frames,
    read_frame_bytes,
)
from seestar_controller.store.project import Project, utc_now_iso
from seestar_controller.store.quality import save_frame_quality

from .metrics import (
    DEFAULT_MAD_THRESHOLD,
    FrameMetrics,
    QualityError,
    flag_outliers,
    flag_reasons,
    score_frame,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScoredFrame:
    """One scored sub-frame of a session.

    - ``frame`` — the stored :class:`~seestar_controller.store.frames.Frame`;
    - ``metrics`` — its :class:`~.metrics.FrameMetrics`;
    - ``flagged`` — the outlier flag (robust z beyond the threshold on any
      metric, per session);
    - ``reasons`` — the metric names that flagged it (e.g. ``("fwhm",)``),
      empty when not flagged.
    """

    frame: Frame
    metrics: FrameMetrics
    flagged: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SkippedFrame:
    """One sub-frame that could not be scored during :func:`score_session`:
    ``frame_id`` + the human-readable ``reason``."""

    frame_id: int
    reason: str


@dataclass(frozen=True)
class SessionScore:
    """The result of :func:`score_session`: the scored frames (in frame
    order) plus the skipped ones, and the flagging threshold used.

    Summary properties: :attr:`frame_count` (scored + skipped),
    :attr:`flagged` (the flagged :class:`ScoredFrame` records),
    :attr:`flagged_count`, :attr:`rejected_percent` (``100 * flagged /
    scored``, 0.0 when nothing was scored) and :attr:`median_fwhm` (None when
    no scored frame has a FWHM).
    """

    session_id: str
    frames: tuple[ScoredFrame, ...]
    skipped: tuple[SkippedFrame, ...]
    mad_threshold: float

    @property
    def frame_count(self) -> int:
        """The total number of sub-frames processed (scored + skipped)."""
        return len(self.frames) + len(self.skipped)

    @property
    def flagged(self) -> tuple[ScoredFrame, ...]:
        """The flagged (rejected) scored frames."""
        return tuple(f for f in self.frames if f.flagged)

    @property
    def flagged_count(self) -> int:
        """The number of flagged (rejected) frames."""
        return len(self.flagged)

    @property
    def rejected_percent(self) -> float:
        """The rejected fraction in percent over the SCORED frames
        (0.0 when nothing was scored)."""
        if not self.frames:
            return 0.0
        return 100.0 * len(self.flagged) / len(self.frames)

    @property
    def median_fwhm(self) -> float | None:
        """The median FWHM (pixels) over the scored frames that have one."""
        values = [f.metrics.fwhm for f in self.frames if f.metrics.fwhm is not None]
        if not values:
            return None
        return float(statistics.median(values))


def score_session(
    project: Project,
    session_id: str,
    *,
    mad_threshold: float = DEFAULT_MAD_THRESHOLD,
    persist: bool = True,
    **score_kwargs: Any,
) -> SessionScore:
    """Score ALL sub-frames of the session ``session_id`` in ``project``,
    flag the per-session outliers, optionally persist the records, and
    return the :class:`SessionScore`.

    - ``session_id`` — must exist in the project, else
      :class:`~seestar_controller.store.errors.SessionNotFoundError`;
    - ``mad_threshold`` — the robust z-score flagging threshold (default
      :data:`~.metrics.DEFAULT_MAD_THRESHOLD` = 3.0), see
      :func:`.metrics.flag_outliers`;
    - ``persist`` — when True (default), every scored frame's metrics + flag
      + reasons are upserted into the project db (one transaction per frame)
      via :func:`seestar_controller.store.quality.save_frame_quality`;
    - ``**score_kwargs`` — forwarded to :func:`.metrics.score_frame`
      (``fwhm``, ``threshold_sigma``, ``sigma_clip``, ``cutout_radius``).

    Each sub-frame (``kind="sub"`` only) is read from the project and scored
    with :func:`.metrics.score_frame`; a frame that cannot be read or scored
    is recorded as a :class:`SkippedFrame` — never a crash. The outliers are
    flagged per session over the scored frames (robust MAD z-score per
    metric, two-sided). Returns the :class:`SessionScore`.
    """
    if not isinstance(session_id, str):
        raise TypeError(f"session_id must be a string, got {session_id!r}")
    if not isinstance(persist, bool):
        raise TypeError(f"persist must be a bool, got {persist!r}")
    row = project.db.execute(
        "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise SessionNotFoundError(
            f"project {project.slug!r} has no session {session_id!r}"
        )

    frames = list_frames(project, session_id=session_id, kind=KIND_SUB)
    scored: list[tuple[Frame, FrameMetrics]] = []
    skipped: list[SkippedFrame] = []
    for frame in frames:
        try:
            fits_bytes = read_frame_bytes(project, frame)
            metrics = score_frame(fits_bytes, **score_kwargs)
        except (ProjectError, QualityError) as exc:
            logger.info("skipping sub-frame %s: %s", frame.id, exc)
            skipped.append(SkippedFrame(frame_id=frame.id, reason=str(exc)))
            continue
        scored.append((frame, metrics))

    reasons = flag_reasons([m for _, m in scored], mad_threshold)
    flagged = flag_outliers([m for _, m in scored], mad_threshold)
    scored_frames = tuple(
        ScoredFrame(
            frame=frame,
            metrics=metrics,
            flagged=i in flagged,
            reasons=reasons.get(i, ()),
        )
        for i, (frame, metrics) in enumerate(scored)
    )

    if persist:
        scored_at = utc_now_iso()
        for i, (frame, metrics) in enumerate(scored):
            save_frame_quality(
                project,
                frame.id,
                fwhm=metrics.fwhm,
                bg_median=metrics.bg_median,
                bg_std=metrics.bg_std,
                star_count=metrics.star_count,
                flagged=i in flagged,
                flag_reasons=reasons.get(i, ()),
                scored_at=scored_at,
            )

    return SessionScore(
        session_id=session_id,
        frames=scored_frames,
        skipped=tuple(skipped),
        mad_threshold=float(mad_threshold),
    )
