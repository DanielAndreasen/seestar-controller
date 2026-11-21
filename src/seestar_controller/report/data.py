"""Report data collection — the PURE core of session-report ticket 01
(:mod:`seestar_controller.report.data`).

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) — stdlib +
:mod:`seestar_controller.store` + :mod:`seestar_controller.analysis` (the pure
period-analysis fold math) only. No :mod:`seestar_controller.link`, no
:mod:`ui`, no :mod:`photometry`, no :mod:`enhance`, no :mod:`observing`
imports (observing pulls in ``link``; the session log JSONL is read directly
from disk instead — see :data:`SESSION_LOG_FILE`).

What lives here:

- :class:`ReportError` — the error type (a :class:`ValueError`);
- :class:`LogEntry` — one session-log event, parsed from the JSONL file the
  observing capture writes (``subs/<session>/session_log.jsonl``);
- :func:`session_log_path` / :func:`load_session_log` — locate / parse that
  file (corrupt lines are skipped, like the capture's own loader);
- :class:`MeasurementsSummary` — the aggregate stats of the project's
  lightcurve (counts, mag/jd ranges, per-session counts);
- :class:`QualityPoint` / :class:`QualitySummary` — the session's frame
  quality (ticket 02): the persisted per-frame records
  (:func:`seestar_controller.store.quality.session_quality`) joined with
  their frames' ``jd``, plus the summary (median FWHM, % rejected, ...);
- :class:`MoverDetection` / :func:`load_movers` — the session's moving-object
  detections (ticket 02), loaded DEFENSIVELY from
  :mod:`seestar_controller.moving_objects` when that package exists (its
  ticket lands in parallel — when it does not, or it has no data / fails,
  the report simply omits the section);
- :class:`ReportData` — the frozen everything-needed-to-render snapshot of
  one session's report;
- :func:`collect_report_data` — gather it all from an open project + a
  session id: the session record, the session's frames, the session log, the
  project-wide lightcurve (excluded points included, so the plot can mark
  them), the current period from ``project.json`` (validated; a corrupt
  value is ignored) with the folded points, the measurements summary, and
  the "best" stack frame of the session (the latest ``enhanced`` frame, else
  the latest ``stack``) for the thumbnail.

Documented choices:

- **Session log on disk, not via observing.** The store has no session-events
  API and :mod:`seestar_controller.observing` imports :mod:`link`, so the
  report reads ``subs/<session_id>/session_log.jsonl`` directly (the format
  is the capture's :class:`~seestar_controller.observing.capture.LogEvent`
  JSONL: one JSON object per line with ``ts``/``kind``/``detail``/``source``/
  ``state``/``frame_id``). A missing file is not an error — it yields ``()``
  ("no events recorded"); a corrupt line is skipped, exactly like
  ``SessionLog.load``.
- **Period from project.json.** The current period is
  ``project.config['period']`` (a plain float in days or null — persisted by
  period-analysis). It is validated with
  :func:`seestar_controller.analysis.fold.validate_period`; a corrupt value
  is ignored (period None), never a crash — same policy as
  :class:`~seestar_controller.analysis.fold.PeriodController`. The fold uses
  :func:`seestar_controller.analysis.fold.fold_points` with the default epoch
  (the first observation = min jd), over ALL lightcurve points including
  excluded ones (matching the period controller's snapshot).
- **Lightcurve is project-wide.** The report shows the whole campaign's
  lightcurve (all sessions, excluded included and marked), like the
  lightcurve view; the session's own frames/log are the per-session sections.
- **Best stack = latest enhanced, else latest stack.** The thumbnail section
  prefers the newest ``enhanced`` frame of the session (the image-enhance
  pipeline output), falling back to the newest ``stack``; ``None`` when the
  session has neither (the HTML then shows a placeholder, never a crash).
- **Quality + movers are optional sections (ticket 02).** The frame-quality
  summary is read from the store's persisted quality records
  (:func:`seestar_controller.store.quality.session_quality` — the same API
  :mod:`seestar_controller.frame_quality` re-exports; the store is imported
  directly so this package never pulls in :mod:`frame_quality`'s widget
  layer). A session with no scored frames yields ``quality=None`` and the
  report OMITS the quality section (missing data -> gracefully omitted, never
  a placeholder crash). The movers are loaded from
  :mod:`seestar_controller.moving_objects` **defensively**: the import is
  lazy and any failure (package absent, no ``session_movers`` API, an
  exception, an empty result) yields ``movers=None`` and the section is
  omitted.
"""

from __future__ import annotations

import importlib
import json
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from seestar_controller.analysis.fold import (
    FoldedPoint,
    FoldError,
    fold_points,
    validate_period,
)
from seestar_controller.store.frames import (
    KIND_ENHANCED,
    KIND_STACK,
    Frame,
    list_frames,
)
from seestar_controller.store.measurements import Measurement, lightcurve
from seestar_controller.store.project import SUBS_DIR, Project
from seestar_controller.store.quality import session_quality
from seestar_controller.store.sessions import Session, list_sessions


class ReportError(ValueError):
    """A report failure: an unknown session id, a bad argument, or a store
    read failure while collecting the report data — always a clean
    :class:`ValueError`, never a crash."""


#: File name of the per-session event log inside ``subs/<session_id>/`` —
#: the observing capture's constant (``observing/capture.py``), re-declared
#: here because this package must not import :mod:`seestar_controller.observing`
#: (it imports :mod:`link`).
SESSION_LOG_FILE = "session_log.jsonl"

#: The project.json config key holding the current period (days, float or
#: null) — period-analysis' constant, re-declared for the same reason (the
#: analysis package is pure and could be imported, but the key is read via
#: the config dict directly here).
PERIOD_CONFIG_KEY = "period"


def session_log_path(project: Project, session_id: str) -> Path:
    """The path of the session event log:
    ``<project>/subs/<session_id>/session_log.jsonl``."""
    return Path(project.path) / SUBS_DIR / session_id / SESSION_LOG_FILE


@dataclass(frozen=True)
class LogEntry:
    """One session-log event (parsed from the JSONL file).

    - ``ts`` — wall-clock timestamp (epoch seconds, :func:`time.time`);
    - ``kind`` — the event kind (``goto``/``stack``/``error``/``frame``/
      ``session``/``capture``/``executor``);
    - ``detail`` — human-readable detail;
    - ``source`` — ``executor`` or ``capture``;
    - ``state`` — the executor state when mirrored, else ``""``;
    - ``frame_id`` — the Scope frame id for ``frame`` events, else None.
    """

    ts: float
    kind: str
    detail: str
    source: str
    state: str
    frame_id: str | None

    @property
    def utc(self) -> str:
        """``ts`` as a UTC ISO-8601 string (``+00:00``, second precision)."""
        return datetime.fromtimestamp(self.ts, UTC).isoformat(timespec="seconds")


def load_session_log(project: Project, session_id: str) -> tuple[LogEntry, ...]:
    """Parse the session event log of ``session_id`` into :class:`LogEntry`\\ s,
    in file order (append order).

    A missing log file yields ``()`` (no events recorded yet); a corrupt line
    (not JSON, or missing/invalid fields) is skipped — the same policy as the
    capture's own ``SessionLog.load``. Raises :class:`ReportError` when the
    path exists but is not readable.
    """
    path = session_log_path(project, session_id)
    if not path.is_file():
        return ()
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReportError(f"cannot read the session log {path}: {exc}") from exc
    entries: list[LogEntry] = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            entries.append(
                LogEntry(
                    ts=float(raw["ts"]),
                    kind=str(raw["kind"]),
                    detail=str(raw.get("detail", "")),
                    source=str(raw.get("source", "")),
                    state=str(raw.get("state", "")),
                    frame_id=None if raw.get("frame_id") is None else str(raw["frame_id"]),
                )
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue  # corrupt line — skipped, like SessionLog.load
    return tuple(entries)


@dataclass(frozen=True)
class MeasurementsSummary:
    """Aggregate stats of the project's lightcurve (all sessions, excluded
    points included).

    - ``n_points`` / ``n_excluded`` — total points / flagged outliers;
    - ``n_sessions`` — how many sessions have at least one point;
    - ``per_session`` — ``(session_id, count)`` pairs in first-seen (jd)
      order;
    - ``mag_min`` / ``mag_max`` / ``mag_mean`` / ``mag_std`` — the mag stats
      (None when there are no points);
    - ``jd_min`` / ``jd_max`` — the time span as Julian Dates (None when
      empty).
    """

    n_points: int
    n_excluded: int
    n_sessions: int
    per_session: tuple[tuple[str, int], ...]
    mag_min: float | None
    mag_max: float | None
    mag_mean: float | None
    mag_std: float | None
    jd_min: float | None
    jd_max: float | None


def summarize_measurements(points: tuple[Measurement, ...]) -> MeasurementsSummary:
    """Build the :class:`MeasurementsSummary` of ``points`` (already ordered
    by jd)."""
    n = len(points)
    n_excluded = sum(1 for p in points if p.excluded)
    order: list[str] = []
    counts: dict[str, int] = {}
    for p in points:
        if p.session_id not in counts:
            counts[p.session_id] = 0
            order.append(p.session_id)
        counts[p.session_id] += 1
    per_session = tuple((sid, counts[sid]) for sid in order)
    if not points:
        return MeasurementsSummary(
            n_points=0,
            n_excluded=0,
            n_sessions=0,
            per_session=(),
            mag_min=None,
            mag_max=None,
            mag_mean=None,
            mag_std=None,
            jd_min=None,
            jd_max=None,
        )
    mags = [p.mag for p in points]
    jds = [p.jd for p in points]
    return MeasurementsSummary(
        n_points=n,
        n_excluded=n_excluded,
        n_sessions=len(counts),
        per_session=per_session,
        mag_min=min(mags),
        mag_max=max(mags),
        mag_mean=statistics.fmean(mags),
        mag_std=statistics.pstdev(mags) if n > 1 else 0.0,
        jd_min=min(jds),
        jd_max=max(jds),
    )


@dataclass(frozen=True)
class QualityPoint:
    """One scored frame of the session's quality table, joined with its
    frame's ``jd`` (the persisted :class:`~seestar_controller.store.quality.FrameQuality`
    record carries no jd — the join happens here, like
    :func:`seestar_controller.frame_quality.trends.session_trends`).

    - ``frame_id`` — the ``frames`` row id;
    - ``jd`` — the frame's observation time as Julian Date;
    - ``fwhm`` — the estimated FWHM in pixels, or None when no star was
      detected on the frame;
    - ``bg_median`` / ``bg_std`` — the sigma-clipped background median/std;
    - ``star_count`` — the detected star count;
    - ``flagged`` — the outlier flag; ``flag_reasons`` — the metric names
      that flagged the frame.
    """

    frame_id: int
    jd: float
    fwhm: float | None
    bg_median: float
    bg_std: float
    star_count: int
    flagged: bool
    flag_reasons: tuple[str, ...]


@dataclass(frozen=True)
class QualitySummary:
    """The session's frame-quality summary for the report (ticket 02),
    built from :func:`seestar_controller.store.quality.session_quality`
    joined with the session frames.

    - ``session_id`` — the session the records belong to;
    - ``points`` — the :class:`QualityPoint`\\ s, ordered by ``jd``.

    Summary properties: :attr:`frame_count`, :attr:`flagged` (the flagged
    points), :attr:`flagged_count`, :attr:`rejected_percent` (``100 *
    flagged / total``, 0.0 when empty), :attr:`median_fwhm` (None when no
    point has a FWHM), :attr:`median_bg_std`, :attr:`median_star_count`.
    """

    session_id: str
    points: tuple[QualityPoint, ...]

    @property
    def frame_count(self) -> int:
        """The number of scored frames in the session."""
        return len(self.points)

    @property
    def flagged(self) -> tuple[QualityPoint, ...]:
        """The flagged (rejected) points."""
        return tuple(p for p in self.points if p.flagged)

    @property
    def flagged_count(self) -> int:
        """The number of flagged (rejected) frames."""
        return len(self.flagged)

    @property
    def rejected_percent(self) -> float:
        """The rejected fraction in percent (0.0 when empty)."""
        if not self.points:
            return 0.0
        return 100.0 * len(self.flagged) / len(self.points)

    @property
    def median_fwhm(self) -> float | None:
        """The median FWHM (pixels) over the points that have one, or None."""
        values = [p.fwhm for p in self.points if p.fwhm is not None]
        if not values:
            return None
        return float(statistics.median(values))

    @property
    def median_bg_std(self) -> float | None:
        """The median background std over the scored frames, or None."""
        if not self.points:
            return None
        return float(statistics.median(p.bg_std for p in self.points))

    @property
    def median_star_count(self) -> float | None:
        """The median detected star count over the scored frames, or None."""
        if not self.points:
            return None
        return float(statistics.median(p.star_count for p in self.points))


@dataclass(frozen=True)
class MoverDetection:
    """One detected moving object of the session (ticket 02), coerced from
    a duck-typed moving-objects record.

    - ``kind`` — the detection kind (``asteroid`` / ``satellite`` / ...);
    - ``label`` — the human-readable name / id;
    - ``position`` — a short position string (sky ``ra/dec`` or pixel
      ``x/y``), empty when unknown;
    - ``frames`` — a short "how many frames" string, empty when unknown.
    """

    kind: str
    label: str
    position: str
    frames: str


def _coerce_mover(raw: Any) -> MoverDetection:
    """Coerce one duck-typed moving-objects record into a
    :class:`MoverDetection` (attribute aliases: ``kind``/``type``,
    ``label``/``name``/``id``, ``ra_deg``/``dec_deg`` or ``x``/``y``,
    ``n_frames``/``frame_count``/``frames``)."""
    kind = getattr(raw, "kind", None) or getattr(raw, "type", None) or "mover"
    label = (
        getattr(raw, "label", None)
        or getattr(raw, "name", None)
        or getattr(raw, "id", None)
        or ""
    )
    ra = getattr(raw, "ra_deg", None)
    dec = getattr(raw, "dec_deg", None)
    x = getattr(raw, "x", None)
    y = getattr(raw, "y", None)
    position = ""
    try:
        if ra is not None and dec is not None:
            position = f"ra {float(ra):.5f}° dec {float(dec):+.5f}°"
        elif x is not None and y is not None:
            position = f"x {float(x):.1f} px, y {float(y):.1f} px"
    except (TypeError, ValueError):
        position = ""
    n_frames = (
        getattr(raw, "n_frames", None)
        if getattr(raw, "n_frames", None) is not None
        else getattr(raw, "frame_count", getattr(raw, "frames", None))
    )
    frames = ""
    if isinstance(n_frames, bool):
        frames = ""
    elif isinstance(n_frames, (int, float)):
        frames = f"{int(n_frames)} frame(s)"
    elif n_frames:
        frames = str(n_frames)
    return MoverDetection(
        kind=str(kind), label=str(label), position=position, frames=frames
    )


def load_movers(project: Project, session_id: str) -> tuple[MoverDetection, ...] | None:
    """The session's moving-object detections as :class:`MoverDetection`\\ s,
    or **None when there is no mover data** (the report then omits the
    movers section).

    Defensive by design (the moving-objects package is a parallel
    workstream): the import of :mod:`seestar_controller.moving_objects` is
    LAZY and any failure — the package absent, no ``session_movers`` API, an
    exception from the call, an empty result — yields None. Nothing here
    ever raises.
    """
    if not isinstance(session_id, str):
        raise TypeError(f"session_id must be a string, got {session_id!r}")
    try:
        # importlib (not ``from seestar_controller import moving_objects``):
        # the package is a parallel workstream and may not exist yet, and a
        # dynamic import keeps the static checker happy about the missing
        # module while still honouring a sys.modules entry (the tests inject
        # a fake module there)
        moving_objects = importlib.import_module("seestar_controller.moving_objects")
    except ImportError:
        return None
    getter = getattr(moving_objects, "session_movers", None)
    if not callable(getter):
        return None
    try:
        raw = getter(project, session_id)
    except Exception:  # a failing mover lookup never breaks the report  # noqa: BLE001
        return None
    if raw is None:
        return None
    try:
        movers = tuple(_coerce_mover(item) for item in raw)
    except Exception:  # a malformed record never breaks the report  # noqa: BLE001
        return None
    return movers or None  # no detections -> None -> section omitted


def _collect_quality(
    project: Project, session_id: str, frames: tuple[Frame, ...]
) -> QualitySummary | None:
    """The session's :class:`QualitySummary` from the persisted quality
    records joined with the frames' ``jd``, or None when the session has no
    scored frames (or the read fails — missing data is omitted, never a
    crash)."""
    try:
        table = session_quality(project, session_id)
    except Exception:  # no readable quality data -> the section is omitted  # noqa: BLE001
        return None
    if not table.frames:
        return None
    by_id = {frame.id: frame for frame in frames}
    points: list[QualityPoint] = []
    for record in table.frames:
        frame = by_id.get(record.frame_id)
        if frame is None:
            continue  # a record whose frame is not in this session's list
        points.append(
            QualityPoint(
                frame_id=record.frame_id,
                jd=frame.jd,
                fwhm=record.fwhm,
                bg_median=record.bg_median,
                bg_std=record.bg_std,
                star_count=record.star_count,
                flagged=record.flagged,
                flag_reasons=tuple(record.flag_reasons),
            )
        )
    if not points:
        return None
    points.sort(key=lambda p: p.jd)
    return QualitySummary(session_id=session_id, points=tuple(points))


@dataclass(frozen=True)
class ReportData:
    """The frozen everything-needed-to-render snapshot of one session's
    report (built by :func:`collect_report_data`).

    - ``project_name`` / ``project_slug`` — from ``project.json``;
    - ``session_id`` / ``started_at`` / ``ended_at`` — the session record;
    - ``generated_at`` — the report generation time (UTC ISO-8601);
    - ``target`` — the target name (project config's ``target``, else the
      first frame's ``target`` metadata), or None;
    - ``frames`` — the session's frames (all kinds), ordered by jd;
    - ``log`` — the session's event log entries (file order);
    - ``lightcurve`` — the project-wide lightcurve (all sessions, excluded
      included), ordered by jd;
    - ``period`` — the current period in days (from ``project.json``), or
      None when none/invalid;
    - ``folded`` — the lightcurve folded by ``period`` (sorted by phase),
      or ``()`` when no period / no points;
    - ``summary`` — the :class:`MeasurementsSummary` of the lightcurve;
    - ``stack_frame`` — the session's best stack frame for the thumbnail
      (latest ``enhanced``, else latest ``stack``), or None;
    - ``quality`` — the session's :class:`QualitySummary` (ticket 02), or
      None when the session has no scored frames — the report then OMITS
      the quality section;
    - ``movers`` — the session's :class:`MoverDetection`\\ s (ticket 02), or
      None when no moving-objects data exists — the report then OMITS the
      movers section.
    """

    project_name: str
    project_slug: str
    session_id: str
    started_at: str
    ended_at: str | None
    generated_at: str
    target: str | None
    frames: tuple[Frame, ...]
    log: tuple[LogEntry, ...]
    lightcurve: tuple[Measurement, ...]
    period: float | None
    folded: tuple[FoldedPoint, ...]
    summary: MeasurementsSummary
    stack_frame: Frame | None
    quality: QualitySummary | None = None
    movers: tuple[MoverDetection, ...] | None = None


def _check_project(project: Any) -> Project:
    """Duck-type check: ``project`` must be a store project handle (``db`` /
    ``config`` / ``path`` attributes); :class:`TypeError` otherwise."""
    if not all(hasattr(project, attr) for attr in ("db", "config", "path")):
        raise TypeError(
            "project must be an open seestar_controller.store Project handle "
            f"(with db/config/path), got {type(project).__name__}"
        )
    return project


def _check_session_id(session_id: Any) -> str:
    if not isinstance(session_id, str):
        raise TypeError(f"session_id must be a string, got {session_id!r}")
    return session_id


def _find_session(project: Project, session_id: str) -> Session:
    """The session record ``session_id`` of ``project``; :class:`ReportError`
    when no such session exists."""
    for session in list_sessions(project):
        if session.id == session_id:
            return session
    raise ReportError(f"project {project.slug!r} has no session {session_id!r}")


def _target_name(project: Project, frames: tuple[Frame, ...]) -> str | None:
    """The report's target name: the project config's ``target`` (a string, or
    a mapping with a ``name`` entry), else the first frame's ``target``
    metadata, else None."""
    raw = project.config.get("target")
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, dict):
        name = raw.get("name")
        if isinstance(name, str) and name:
            return name
    for frame in frames:
        if frame.target:
            return frame.target
    return None


def _current_period(project: Project) -> float | None:
    """The current period from ``project.config['period']`` (days), validated
    with :func:`~seestar_controller.analysis.fold.validate_period`; None when
    unset or corrupt (never a crash)."""
    raw = project.config.get(PERIOD_CONFIG_KEY)
    if raw is None:
        return None
    try:
        return validate_period(raw)
    except FoldError:
        return None  # corrupt persisted value — ignored, like PeriodController


def _best_stack_frame(frames: tuple[Frame, ...]) -> Frame | None:
    """The thumbnail frame: the latest ``enhanced`` frame, else the latest
    ``stack`` (``frames`` are jd-ordered, so the last match wins), else None."""
    for kind in (KIND_ENHANCED, KIND_STACK):
        matches = [f for f in frames if f.kind == kind]
        if matches:
            return matches[-1]
    return None


def _default_clock() -> datetime:
    return datetime.now(UTC)


def collect_report_data(
    project: Project,
    session_id: str,
    *,
    clock: Callable[[], datetime] | None = None,
) -> ReportData:
    """Gather the :class:`ReportData` of ``session_id`` in ``project``.

    - ``project`` — an open store :class:`~seestar_controller.store.project.Project`
      handle (duck-typed: ``db``/``config``/``path``); :class:`TypeError`
      otherwise;
    - ``session_id`` — a string; :class:`TypeError` otherwise, and
      :class:`ReportError` when the project has no such session;
    - ``clock`` — injectable clock for the ``generated_at`` timestamp (tests
      pass a frozen clock); default: the system UTC clock.

    Store read failures are wrapped in :class:`ReportError` (chained).
    """
    project = _check_project(project)
    session_id = _check_session_id(session_id)
    try:
        session = _find_session(project, session_id)
        frames = tuple(list_frames(project, session_id=session_id))
        points = tuple(lightcurve(project, include_excluded=True))
    except ReportError:
        raise
    except Exception as exc:
        raise ReportError(
            f"cannot collect the report data of session {session_id!r} in project "
            f"{project.slug!r}: {exc}"
        ) from exc
    log = load_session_log(project, session_id)
    period = _current_period(project)
    folded: tuple[FoldedPoint, ...] = ()
    if period is not None and points:
        try:
            folded = fold_points(points, period)  # jd0 = first observation
        except FoldError:
            folded = ()  # cannot happen with a validated period + points
    now = (clock or _default_clock)()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return ReportData(
        project_name=project.name,
        project_slug=project.slug,
        session_id=session.id,
        started_at=session.started_at,
        ended_at=session.ended_at,
        generated_at=now.astimezone(UTC).isoformat(timespec="milliseconds"),
        target=_target_name(project, frames),
        frames=frames,
        log=log,
        lightcurve=points,
        period=period,
        folded=folded,
        summary=summarize_measurements(points),
        stack_frame=_best_stack_frame(frames),
        quality=_collect_quality(project, session.id, frames),
        movers=load_movers(project, session.id),
    )
