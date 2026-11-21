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

- :mod:`.trends` — the pure trend computation of ticket 02: the airmass of
  the session target from its coords + the frame times
  (:func:`~.trends.airmass_series`, astropy ``AltAz`` with a LAZY import),
  the per-session series extraction from the stored metrics
  (:func:`~.trends.session_trends` via
  :func:`seestar_controller.store.quality.session_quality`), the pure plot
  transform math (:class:`~.trends.TrendWindow` /
  :func:`~.trends.full_trend_window` / :func:`~.trends.trend_to_screen` /
  :func:`~.trends.screen_to_trend`), and the app-level shared
  :class:`~.trends.QualityController` (frozen
  :class:`~.trends.QualitySnapshot` with the sessions, the selected session's
  :class:`~.trends.SessionTrends` and a never-raising ``error``; listeners
  marshalled via ``invoke_on_main``);
- :mod:`.quality_view` — the WIDGET layer of ticket 02:
  :class:`~.quality_view.SessionQualityView` (session selector + Refresh +
  the per-session summary card + the three trend plots FWHM / background /
  airmass vs time, reusing the analysis lightcurve view's plotting widgets)
  and :class:`~.quality_view.TrendPlot`. It imports ``gi`` at module top and
  is re-exported here **lazily** (PEP 562 ``__getattr__``), so
  ``import seestar_controller.frame_quality`` never imports ``gi``.

Ticket 01 covers the metrics core + flagging + storage; ticket 02 the
quality-trends view; ticket 03 the photometry exclusion of flagged frames
(:func:`flagged_frame_reasons` is the glue the photometry run consumes as its
default excluded-frame set).
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
    flagged_frame_reasons,
    score_session,
)
from .trends import (
    DB_CALL_TIMEOUT_S,
    DEFAULT_OBSERVER,
    DEFAULT_TREND_WINDOW,
    MAX_AIRMASS,
    Observer,
    QualityController,
    QualitySnapshot,
    SessionTrends,
    TrendPoint,
    TrendsError,
    TrendWindow,
    airmass_series,
    full_trend_window,
    resolve_target_coords,
    screen_to_trend,
    session_trends,
    trend_to_screen,
)

# "SessionQualityView" is the widget layer (lazy, imports gi — see
# _WIDGET_EXPORTS below); everything else is pure and importable headless.
__all__ = [
    "DB_CALL_TIMEOUT_S",
    "DEFAULT_FWHM_PX",
    "DEFAULT_MAD_THRESHOLD",
    "DEFAULT_OBSERVER",
    "DEFAULT_THRESHOLD_SIGMA",
    "DEFAULT_TREND_WINDOW",
    "FWHM_CUTOUT_RADIUS_PX",
    "MAD_SCALE",
    "MAX_AIRMASS",
    "MAX_FWHM_STARS",
    "METRIC_FIELDS",
    "SIGMA_CLIP",
    "FrameMetrics",
    "FrameQuality",
    "Observer",
    "QualityController",
    "QualityError",
    "QualitySnapshot",
    "ScoredFrame",
    "SessionQuality",
    "SessionQualityView",
    "SessionScore",
    "SessionTrends",
    "SkippedFrame",
    "TrendPoint",
    "TrendWindow",
    "TrendsError",
    "airmass_series",
    "flag_outliers",
    "flag_reasons",
    "flagged_frame_reasons",
    "frame_quality_for_frame",
    "full_trend_window",
    "resolve_target_coords",
    "robust_zscores",
    "save_frame_quality",
    "score_frame",
    "score_session",
    "screen_to_trend",
    "session_quality",
    "session_trends",
    "trend_to_screen",
]

#: Widget-layer names re-exported lazily (importing the module pulls in
#: ``gi`` — only the GUI path does).
_WIDGET_EXPORTS = {
    "SessionQualityView": "quality_view",
}


def __getattr__(name: str):
    if name in _WIDGET_EXPORTS:
        from importlib import import_module

        module = import_module(f".{_WIDGET_EXPORTS[name]}", __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
