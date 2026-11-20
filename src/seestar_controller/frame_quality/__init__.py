"""Frame quality — per-sub-frame quality metrics, outlier flagging, and
per-session persistence (``.scratch/frame-quality/spec.md``).

Strict pure layer (the sandbox has no GTK, see docs/wiki/assumptions.md):
NOTHING in this package imports ``gi`` — a subprocess test enforces the
boundary. The package is stdlib + :mod:`numpy` + :mod:`seestar_controller.store`
+ :mod:`seestar_controller.frame_quality.metrics` / :mod:`.session`; the heavy
third-party imports (:mod:`astropy.io.fits`, :mod:`astropy.stats`,
:mod:`photutils.detection`) are lazy inside
:func:`~.metrics.score_frame`, the same pattern as
:mod:`seestar_controller.photometry.measure`.

- :mod:`.metrics` — the pure scoring + flagging core:
  :func:`~.metrics.score_frame` (one frame — a FITS path, FITS bytes, or a
  raw array — to a :class:`~.metrics.FrameMetrics`: FWHM via
  ``photutils.detection.DAOStarFinder`` + a moment estimator, background
  median/std via :func:`astropy.stats.sigma_clipped_stats`, star count),
  :func:`~.metrics.flag_outliers` / :func:`~.metrics.flag_reasons` (the
  robust MAD z-score flagging per metric, threshold 3.0 by default) and
  :func:`~.metrics.robust_zscores` (the pure math). Errors are
  :class:`~.metrics.QualityError` (a :class:`ValueError`, deliberately
  distinct from the store's own ``QualityError``).
- :mod:`.session` — :func:`~.session.score_session` (score ALL sub-frames of
  one session of a project, flag the per-session outliers, persist the
  records via the store) returning a frozen :class:`~.session.SessionScore`
  with :class:`~.session.ScoredFrame` / :class:`~.session.SkippedFrame`
  records.

Persistence lives in the store (schema v4, migration 3 -> 4, see
:mod:`seestar_controller.store.schema`): the ``frame_quality`` table holds one
record per frame (metrics + outlier flag + reasons + scoring time), written
by :func:`seestar_controller.store.quality.save_frame_quality` and read back
by :func:`seestar_controller.store.quality.session_quality` — re-exported
here as :func:`session_quality` — which returns a
:class:`~seestar_controller.store.quality.SessionQuality` (the session's
metrics table ordered by ``jd`` plus the summary: median FWHM, % rejected,
...).

Typical flow::

    from seestar_controller.frame_quality import score_session, session_quality

    score = score_session(project, session_id)      # scores + flags + persists
    quality = session_quality(project, session_id)  # the persisted table + summary
    quality.median_fwhm, quality.rejected_percent

Ticket 01 covers the metrics core + flagging + storage; ticket 02 the
quality-trends view; ticket 03 the photometry exclusion of flagged frames.
"""

from seestar_controller.store.quality import (
    FrameQuality,
    SessionQuality,
    frame_quality_for_frame,
    save_frame_quality,
    session_quality,
)

from .metrics import (
    DEFAULT_FWHM_PX,
    DEFAULT_MAD_THRESHOLD,
    DEFAULT_THRESHOLD_SIGMA,
    FWHM_CUTOUT_RADIUS_PX,
    MAD_SCALE,
    MAX_FWHM_STARS,
    METRIC_FIELDS,
    SIGMA_CLIP,
    FrameMetrics,
    QualityError,
    flag_outliers,
    flag_reasons,
    robust_zscores,
    score_frame,
)
from .session import (
    ScoredFrame,
    SessionScore,
    SkippedFrame,
    score_session,
)

__all__ = [
    "DEFAULT_FWHM_PX",
    "DEFAULT_MAD_THRESHOLD",
    "DEFAULT_THRESHOLD_SIGMA",
    "FWHM_CUTOUT_RADIUS_PX",
    "MAD_SCALE",
    "MAX_FWHM_STARS",
    "METRIC_FIELDS",
    "SIGMA_CLIP",
    "FrameMetrics",
    "FrameQuality",
    "QualityError",
    "ScoredFrame",
    "SessionQuality",
    "SessionScore",
    "SkippedFrame",
    "flag_outliers",
    "flag_reasons",
    "frame_quality_for_frame",
    "robust_zscores",
    "save_frame_quality",
    "score_frame",
    "score_session",
    "session_quality",
]
