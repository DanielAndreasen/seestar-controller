"""Project integration — plate-solve a stored frame of a project.

Pure-Python (module boundary per ``.scratch/astrometry/spec.md``): no GTK
imports, no :mod:`seestar_controller.link` imports; the pure
:mod:`seestar_controller.store` only. astropy enters lazily inside
:func:`~.solve.solve_frame` / :func:`~.wcs.write_wcs_to_fits`.

- :func:`solve_project_frame` — solve ONE stored frame (a
  :class:`~seestar_controller.store.frames.Frame` or its row id): read the
  FITS bytes from the project, :func:`~.solve.solve_frame` them, write the
  solved WCS back into the stored FITS header (atomically,
  :func:`~.wcs.write_wcs_to_fits`), persist the WCS record in the project db
  (:func:`seestar_controller.store.wcs.save_frame_wcs`, schema v7), and —
  when the Gaia solver produced cross-match rows — persist those too
  (:func:`seestar_controller.store.crossmatch.save_crossmatch`);
- :func:`solve_session_frame` — the same for one frame of one session
  (unknown session -> :class:`~seestar_controller.store.errors.SessionNotFoundError`,
  unknown frame -> :class:`~seestar_controller.store.errors.FrameStoreError`);
- :func:`solve_session_unsolved` — the BATCH solve (issue 10): plate-solve
  every sub-frame of one session that has NO stored ``frame_wcs`` row yet
  (via :func:`seestar_controller.store.wcs.list_solved_frames`), one
  :func:`solve_project_frame` per unsolved sub, tolerating a per-frame
  :class:`~.errors.SolveError` as a skipped frame (never kills the batch),
  reporting ``on_progress(done, total, frame)`` after each frame (a full
  batch yields ``1..N``) and returning the frozen :class:`SessionSolveReport`
  (``solved`` / ``skipped`` lists). An optional ``stride`` (every Nth unsolved
  sub; default 1 = all) and ``call_store`` marshalling hook (the db-touching
  parts of each per-frame solve run through it — the app passes a
  ``GLib.idle_add``-marshalled callable so a worker thread never touches the
  thread-affine sqlite db, while the slow solve itself stays on the caller);
- :func:`session_wcs_coverage` — the session's WCS coverage
  (``solved`` / ``total`` subs, plus the LOW-CONFIDENCE solves) as a frozen
  :class:`SessionWcsCoverage`, for the "508 of 512 frames solved" display.

Issue 16 — solve QUALITY is surfaced next to the count: a solve is
**low-confidence** (:func:`is_low_confidence_solve`) when it used fewer than
:data:`LOW_CONFIDENCE_MIN_STARS` matched stars, or when the match RMS is NULL
(the solver reported none) or above :data:`LOW_CONFIDENCE_MAX_RMS_ARCSEC`
arcsec — such solves can reproject hundreds of px off with no other sign.
:class:`SessionWcsCoverage` counts them (``low_confidence`` /
``low_confidence_ids``, surfaced in its ``summary`` as
``"512 of 512 subs solved (low-confidence: 512)"``) and
:class:`SessionSolveReport` marks the newly solved low-confidence frames
(``low_confidence_ids``, surfaced in its ``summary`` too), and the batch
solve logs a warning per low-confidence frame.

Typical flow::

    from seestar_controller.astrometry import solve_session_frame

    result = solve_session_frame(project, session_id, frame_id)
    result.center_ra_deg, result.center_dec_deg, result.rms_arcsec

    from seestar_controller.astrometry import solve_session_unsolved

    report = solve_session_unsolved(project, session_id, on_progress)
    report.summary  # "508 of 512 unsolved subs solved"
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from seestar_controller.store.crossmatch import save_crossmatch
from seestar_controller.store.errors import FrameStoreError, SessionNotFoundError
from seestar_controller.store.frames import (
    KIND_SUB,
    Frame,
    get_frame_path,
    list_frames,
    read_frame_bytes,
)
from seestar_controller.store.project import Project
from seestar_controller.store.wcs import list_solved_frames, save_frame_wcs

from .errors import SolveError
from .solve import SolveResult, read_fits_bytes, solve_frame
from .wcs import write_wcs_to_fits

logger = logging.getLogger(__name__)

#: A store-call marshaller: runs a zero-argument callable and returns its
#: result. ``None`` means "run directly on the calling thread". The app
#: passes a callable that marshals onto the db-owning thread (``GLib.idle_add``
#: + wait — the same pattern as the photometry/astrometry controllers), so
#: the slow batch solve can run on a worker thread while every sqlite access
#: stays on the db-owning thread.
StoreCall = Callable[[Callable[[], Any]], Any]


def _direct_call(fn: Callable[[], Any]) -> Any:
    """The default :data:`StoreCall`: run ``fn`` directly (tests, or callers
    already on the db-owning thread)."""
    return fn()


#: The progress callback of :func:`solve_session_unsolved`:
#: ``on_progress(done, total, frame)`` — called after each attempted frame on
#: the calling thread, so a full batch yields the ``done`` sequence ``1..N``.
SolveProgressCallback = Callable[[int, int, Frame], None]

#: Issue 16 — a solve is LOW-CONFIDENCE when it used fewer than this many
#: matched stars. A 1-star "solve" fits a WCS but reprojects the field
#: hundreds of px off, so it must be surfaced, not silently counted as solved.
LOW_CONFIDENCE_MIN_STARS = 8

#: Issue 16 — a solve is LOW-CONFIDENCE when its match RMS is NULL (the
#: solver reported none) or above this many arcsec.
LOW_CONFIDENCE_MAX_RMS_ARCSEC = 5.0


def is_low_confidence_solve(
    n_stars: int,
    rms_arcsec: float | None,
    *,
    min_stars: int = LOW_CONFIDENCE_MIN_STARS,
    max_rms_arcsec: float = LOW_CONFIDENCE_MAX_RMS_ARCSEC,
) -> bool:
    """True when a solve with ``n_stars`` matched stars and a match RMS of
    ``rms_arcsec`` arcsec (None = the solver reported no RMS) is
    **LOW-CONFIDENCE** (issue 16): fewer than ``min_stars`` matched stars
    (default :data:`LOW_CONFIDENCE_MIN_STARS` = 8), or an RMS that is NULL or
    above ``max_rms_arcsec`` (default :data:`LOW_CONFIDENCE_MAX_RMS_ARCSEC` =
    5.0 arcsec). Such solves are counted as solved but must be surfaced —
    they can reproject the sky positions far off the frame.
    """
    if isinstance(min_stars, bool) or not isinstance(min_stars, int):
        raise TypeError(f"min_stars must be an int, got {min_stars!r}")
    if min_stars < 0:
        raise ValueError(f"min_stars must be >= 0, got {min_stars!r}")
    if isinstance(max_rms_arcsec, bool) or not isinstance(max_rms_arcsec, (int, float)):
        raise TypeError(f"max_rms_arcsec must be a number, got {max_rms_arcsec!r}")
    if n_stars < min_stars:
        return True
    return rms_arcsec is None or rms_arcsec > max_rms_arcsec


@dataclass(frozen=True)
class SolvedFrame:
    """One sub-frame successfully plate-solved by :func:`solve_session_unsolved`.

    - ``frame_id`` — the solved sub-frame's id;
    - ``result`` — its :class:`~.solve.SolveResult` (already persisted: the
      WCS record in the db, the cards in the stored FITS header, the
      cross-match rows when the Gaia solver produced them).
    """

    frame_id: int
    result: SolveResult


@dataclass(frozen=True)
class SkippedSolve:
    """One sub-frame that could NOT be plate-solved by
    :func:`solve_session_unsolved` — a per-frame :class:`~.errors.SolveError`,
    recorded and skipped (never kills the batch; nothing is written for it).

    - ``frame_id`` — the skipped sub-frame's id;
    - ``reason`` — why it could not be solved (the :class:`SolveError`
      message).
    """

    frame_id: int
    reason: str


@dataclass(frozen=True)
class SessionSolveReport:
    """The frozen result of :func:`solve_session_unsolved`.

    - ``session_id`` — the session that was batch-solved;
    - ``solved`` — the :class:`SolvedFrame`\\ s, in frame order (jd order);
    - ``skipped`` — the :class:`SkippedSolve`\\ s, in frame order.

    Issue 16: the newly solved LOW-CONFIDENCE frames (n_stars below
    :data:`LOW_CONFIDENCE_MIN_STARS`, or rms_arcsec NULL / above
    :data:`LOW_CONFIDENCE_MAX_RMS_ARCSEC`) are marked via
    :attr:`low_confidence_ids` and surfaced in :attr:`summary`.
    """

    session_id: str
    solved: tuple[SolvedFrame, ...]
    skipped: tuple[SkippedSolve, ...]

    @property
    def solved_ids(self) -> tuple[int, ...]:
        """The ids of the newly solved sub-frames, in frame order."""
        return tuple(s.frame_id for s in self.solved)

    @property
    def skipped_ids(self) -> tuple[int, ...]:
        """The ids of the skipped sub-frames, in frame order."""
        return tuple(s.frame_id for s in self.skipped)

    @property
    def low_confidence_ids(self) -> tuple[int, ...]:
        """The ids of the newly solved sub-frames whose solve is
        LOW-CONFIDENCE (issue 16: :func:`is_low_confidence_solve` on the
        frame's :class:`~.solve.SolveResult`), in frame order."""
        return tuple(
            s.frame_id
            for s in self.solved
            if is_low_confidence_solve(s.result.n_stars, s.result.rms_arcsec)
        )

    @property
    def low_confidence(self) -> int:
        """The number of newly solved LOW-CONFIDENCE sub-frames (issue 16)."""
        return len(self.low_confidence_ids)

    @property
    def attempted(self) -> int:
        """The number of unsolved sub-frames attempted (solved + skipped)."""
        return len(self.solved) + len(self.skipped)

    @property
    def summary(self) -> str:
        """A human-readable one-line batch summary: ``"4 of 5 unsolved subs
        solved (1 skipped)"``, with the low-confidence solves (issue 16)
        appended when there are any — e.g. ``"4 of 4 unsolved subs solved
        (4 low-confidence)"`` or ``"3 of 5 unsolved subs solved (2 skipped,
        3 low-confidence)"`` — or ``"no unsolved subs in session '...'"``."""
        attempted = self.attempted
        if attempted == 0:
            return f"no unsolved subs in session {self.session_id!r}"
        text = f"{len(self.solved)} of {attempted} unsolved subs solved"
        notes = []
        if self.skipped:
            notes.append(f"{len(self.skipped)} skipped")
        low = self.low_confidence
        if low:
            notes.append(f"{low} low-confidence")
        if notes:
            text += f" ({', '.join(notes)})"
        return text


@dataclass(frozen=True)
class SessionWcsCoverage:
    """The WCS coverage of one session's sub-frames (issue 10): how many subs
    have a stored ``frame_wcs`` row — and how many of those solves are
    LOW-CONFIDENCE (issue 16).

    - ``session_id`` — the session;
    - ``solved`` — the subs of the session with a stored WCS record;
    - ``total`` — all sub-frames of the session (``kind="sub"``);
    - ``low_confidence`` — the solved subs whose stored solve is
      low-confidence (:func:`is_low_confidence_solve`: n_stars below
      :data:`LOW_CONFIDENCE_MIN_STARS`, or rms_arcsec NULL / above
      :data:`LOW_CONFIDENCE_MAX_RMS_ARCSEC`);
    - ``low_confidence_ids`` — the frame ids of those low-confidence solves,
      in frame order (jd order).
    """

    session_id: str
    solved: int
    total: int
    low_confidence: int = 0
    low_confidence_ids: tuple[int, ...] = ()

    @property
    def unsolved(self) -> int:
        """The subs of the session without a stored WCS record."""
        return max(self.total - self.solved, 0)

    @property
    def high_confidence(self) -> int:
        """The solved subs whose solve is NOT low-confidence
        (``solved - low_confidence``)."""
        return max(self.solved - self.low_confidence, 0)

    @property
    def fraction(self) -> float:
        """The solved fraction (0.0 when the session has no subs)."""
        return (self.solved / self.total) if self.total else 0.0

    @property
    def summary(self) -> str:
        """A human-readable one-line coverage: ``"4 of 512 subs solved"``,
        with the low-confidence solves (issue 16) appended when there are
        any — e.g. ``"512 of 512 subs solved (low-confidence: 512)"`` — or
        ``"no subs in the session"``."""
        if self.total == 0:
            return "no subs in the session"
        text = f"{self.solved} of {self.total} subs solved"
        if self.low_confidence:
            text += f" (low-confidence: {self.low_confidence})"
        return text


def _resolve_frame(project: Project, frame: Frame | int) -> Frame:
    """A :class:`Frame` or a frame row id -> the :class:`Frame`; raises
    :class:`FrameStoreError` naming the id when the project has no such
    frame row."""
    if isinstance(frame, Frame):
        known = {f.id for f in list_frames(project)}
        if frame.id not in known:
            raise FrameStoreError(
                f"project {project.slug!r} has no frame {frame.id!r}"
            )
        return frame
    if isinstance(frame, bool) or not isinstance(frame, int):
        raise TypeError(f"frame must be a Frame or a frame id (int), got {frame!r}")
    for f in list_frames(project):
        if f.id == frame:
            return f
    raise FrameStoreError(f"project {project.slug!r} has no frame {frame!r}")


def prepare_project_frame(
    project: Project, frame: Frame | int
) -> tuple[Frame, Any, Any]:
    """Read side of :func:`solve_project_frame`, split out so a caller on a
    worker thread can do ONLY this part on the db-owning thread: resolve the
    frame row and read + parse its stored FITS bytes. Returns the resolved
    :class:`Frame`, the image ``data`` and the ``header`` dict."""
    resolved = _resolve_frame(project, frame)
    data, header = read_fits_bytes(read_frame_bytes(project, resolved))
    return resolved, data, header


def persist_solve_result(
    project: Project,
    resolved: Frame,
    result: SolveResult,
    *,
    write_header: bool = True,
    persist: bool = True,
    save_matches: bool = True,
) -> None:
    """Write side of :func:`solve_project_frame`, split out for the same
    reason: write the solved WCS cards back into the stored FITS header
    (atomic rewrite), upsert the WCS record into the project db, and persist
    Gaia cross-match rows when present."""
    if write_header:
        write_wcs_to_fits(get_frame_path(project, resolved), result.cards)
    if persist:
        save_frame_wcs(
            project,
            resolved.id,
            solver=result.solver,
            n_stars=result.n_stars,
            center_ra_deg=result.center_ra_deg,
            center_dec_deg=result.center_dec_deg,
            cards=result.cards,
            rms_arcsec=result.rms_arcsec,
            solved_at=result.solved_at,
        )
    if save_matches and result.rows:
        save_crossmatch(project, resolved.id, result.rows)


def solve_project_frame(
    project: Project,
    frame: Frame | int,
    *,
    solver: str = "auto",
    write_header: bool = True,
    persist: bool = True,
    save_matches: bool = True,
    call_store: StoreCall | None = None,
    **solve_kwargs: Any,
) -> SolveResult:
    """Plate-solve the stored frame ``frame`` of ``project`` and return the
    :class:`~.solve.SolveResult`.

    - ``frame`` — a :class:`~seestar_controller.store.frames.Frame` or its
      row id; unknown -> :class:`FrameStoreError`;
    - ``solver`` / ``**solve_kwargs`` — forwarded to
      :func:`~.solve.solve_frame` (``solver=``, ``center_hint=``,
      ``pixel_scale_hint=``, ``gaia_query=``, ...);
    - ``write_header`` — True (default): write the solved WCS cards back into
      the stored FITS file's primary header (atomic rewrite);
    - ``persist`` — True (default): upsert the WCS record into the project
      db (``frame_wcs`` table, schema v7) via
      :func:`seestar_controller.store.wcs.save_frame_wcs`;
    - ``save_matches`` — True (default): when the Gaia solver produced
      cross-match rows, persist them via
      :func:`seestar_controller.store.crossmatch.save_crossmatch`;
    - ``call_store`` — None (default): run everything on the calling thread.
      Otherwise a :data:`StoreCall` marshaller: ONLY the db-touching parts
      (frame resolution + FITS read via :func:`prepare_project_frame`, and
      the persist side via :func:`persist_solve_result`) run through it —
      e.g. marshalled onto the db-owning thread from a worker — while the
      (slow) :func:`~.solve.solve_frame` itself stays on the calling thread,
      so a batch caller never blocks the db thread with a solve.

    The solve runs on the frame's stored bytes; a frame that cannot be solved
    raises :class:`~.solve.SolveError` and NOTHING is written (neither the
    FITS header nor the db).
    """
    call = call_store if call_store is not None else _direct_call
    resolved, data, header = call(lambda: prepare_project_frame(project, frame))
    result = solve_frame(data=data, header=header, solver=solver, **solve_kwargs)
    call(
        lambda: persist_solve_result(
            project,
            resolved,
            result,
            write_header=write_header,
            persist=persist,
            save_matches=save_matches,
        )
    )
    return result


def solve_session_frame(
    project: Project,
    session_id: str,
    frame_id: int,
    **kwargs: Any,
) -> SolveResult:
    """Plate-solve the frame ``frame_id`` of the session ``session_id`` of
    ``project`` — :func:`solve_project_frame` with the session/frame lookup.

    Raises :class:`~seestar_controller.store.errors.SessionNotFoundError`
    when the project has no session ``session_id``, and
    :class:`~seestar_controller.store.errors.FrameStoreError` when the
    session has no frame ``frame_id``. ``**kwargs`` are forwarded to
    :func:`solve_project_frame`.
    """
    if not isinstance(session_id, str):
        raise TypeError(f"session_id must be a string, got {session_id!r}")
    row = project.db.execute(
        "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise SessionNotFoundError(
            f"project {project.slug!r} has no session {session_id!r}"
        )
    if isinstance(frame_id, bool) or not isinstance(frame_id, int):
        raise TypeError(f"frame_id must be an int, got {frame_id!r}")
    for f in list_frames(project, session_id=session_id):
        if f.id == frame_id:
            return solve_project_frame(project, f, **kwargs)
    raise FrameStoreError(
        f"session {session_id!r} of project {project.slug!r} has no frame {frame_id!r}"
    )


def _require_session(project: Project, session_id: str) -> None:
    """Raise :class:`SessionNotFoundError` when the project has no session
    ``session_id`` (a lookup, not a filter — same semantics as
    :func:`solve_session_frame` / :func:`~seestar_controller.store.wcs.list_solved_frames`)."""
    if isinstance(session_id, bool) or not isinstance(session_id, str):
        raise TypeError(f"session_id must be a string, got {session_id!r}")
    row = project.db.execute(
        "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise SessionNotFoundError(
            f"project {project.slug!r} has no session {session_id!r}"
        )


def solve_session_unsolved(
    project: Project,
    session_id: str,
    on_progress: SolveProgressCallback | None = None,
    *,
    stride: int = 1,
    solver: str = "auto",
    call_store: StoreCall | None = None,
    **solve_kwargs: Any,
) -> SessionSolveReport:
    """Batch plate-solve every sub-frame of ``session_id`` that has NO stored
    ``frame_wcs`` row yet (issue 10) and return the frozen
    :class:`SessionSolveReport`.

    - ``project`` / ``session_id`` — the project and one of its sessions;
      an unknown session raises
      :class:`~seestar_controller.store.errors.SessionNotFoundError`;
    - ``on_progress`` — optional ``on_progress(done, total, frame)`` callback,
      called after each attempted frame ON THE CALLING THREAD (a full batch
      yields the ``done`` sequence ``1..N`` over the attempted frames);
    - ``stride`` — solve every Nth unsolved sub (default 1 = ALL unsolved
      subs; the drift is smooth so a stride can be enough, but the full
      solve is the default);
    - ``solver`` / ``**solve_kwargs`` — forwarded to
      :func:`~.solve.solve_frame` per frame (``center_hint=``,
      ``pixel_scale_hint=``, ``gaia_query=``, ...);
    - ``call_store`` — None (default): all store access runs on the calling
      thread (tests, or callers already on the db-owning thread). Otherwise a
      :data:`StoreCall` marshaller (the app: ``GLib.idle_add`` + wait) — the
      listing reads AND the db-touching parts of each per-frame
      :func:`solve_project_frame` run through it, so the batch is safe on a
      worker thread while the slow solve itself stays on the caller (the
      db-owning thread / GTK main loop never blocks on a solve).

    Per frame, exactly :func:`solve_project_frame` runs (WCS written into the
    stored FITS header, ``frame_wcs`` record upserted, cross-match rows
    saved). A frame that cannot be solved raises
    :class:`~.solve.SolveError` — it is recorded as a :class:`SkippedSolve`
    on the report and the batch CONTINUES (a per-frame failure never kills
    the batch; nothing is written for a skipped frame). Frames that already
    have a ``frame_wcs`` row (:func:`~seestar_controller.store.wcs.list_solved_frames`)
    are never attempted again.

    Issue 16: a solved frame whose solve is LOW-CONFIDENCE
    (:func:`is_low_confidence_solve` — n_stars below
    :data:`LOW_CONFIDENCE_MIN_STARS`, or rms_arcsec NULL / above
    :data:`LOW_CONFIDENCE_MAX_RMS_ARCSEC`) is still recorded as solved, but
    it is marked in the report (:attr:`SessionSolveReport.low_confidence_ids`,
    surfaced in :attr:`SessionSolveReport.summary`) and logged as a warning.

    The function is synchronous and GTK-free — safe to call from a worker
    thread when ``call_store`` marshals the store access.
    """
    if isinstance(session_id, bool) or not isinstance(session_id, str) or not session_id:
        raise TypeError(f"session_id must be a non-empty string, got {session_id!r}")
    if on_progress is not None and not callable(on_progress):
        raise TypeError(f"on_progress must be callable or None, got {on_progress!r}")
    if isinstance(stride, bool) or not isinstance(stride, int):
        raise TypeError(f"stride must be an int >= 1, got {stride!r}")
    if stride < 1:
        raise ValueError(f"stride must be an int >= 1, got {stride!r}")
    call = call_store if call_store is not None else _direct_call
    call(lambda: _require_session(project, session_id))
    frames = call(lambda: list_frames(project, session_id=session_id, kind=KIND_SUB))
    solved_ids = {
        w.frame_id for w in call(lambda: list_solved_frames(project, session_id))
    }
    targets = [f for f in frames if f.id not in solved_ids]
    if stride > 1:
        targets = targets[::stride]
    total = len(targets)
    solved: list[SolvedFrame] = []
    skipped: list[SkippedSolve] = []
    for index, frame in enumerate(targets):
        try:
            result = solve_project_frame(
                project, frame, solver=solver, call_store=call_store, **solve_kwargs
            )
        except SolveError as exc:
            # per-frame failure: recorded and skipped, never kills the batch
            logger.info(
                "skipping unsolved sub-frame %s of session %s: %s",
                frame.id,
                session_id,
                exc,
            )
            skipped.append(SkippedSolve(frame_id=frame.id, reason=str(exc)))
        else:
            solved.append(SolvedFrame(frame_id=frame.id, result=result))
            if is_low_confidence_solve(result.n_stars, result.rms_arcsec):
                # issue 16: the solve succeeded but is low-confidence —
                # surface it (the report's low_confidence_ids + this log)
                logger.warning(
                    "low-confidence WCS solve of sub-frame %s of session %s: "
                    "n_stars=%s, rms_arcsec=%s — reprojected positions may be "
                    "far off (re-solve with a different solver/index?)",
                    frame.id,
                    session_id,
                    result.n_stars,
                    result.rms_arcsec,
                )
        if on_progress is not None:
            on_progress(index + 1, total, frame)
    return SessionSolveReport(
        session_id=session_id, solved=tuple(solved), skipped=tuple(skipped)
    )


def session_wcs_coverage(
    project: Project,
    session_id: str,
    *,
    call_store: StoreCall | None = None,
    min_stars: int = LOW_CONFIDENCE_MIN_STARS,
    max_rms_arcsec: float = LOW_CONFIDENCE_MAX_RMS_ARCSEC,
) -> SessionWcsCoverage:
    """The WCS coverage of one session's sub-frames (issue 10): how many of
    the session's ``kind="sub"`` frames have a stored ``frame_wcs`` record —
    and how many of those solves are LOW-CONFIDENCE (issue 16). Returns the
    frozen :class:`SessionWcsCoverage` (``solved`` / ``total`` /
    ``low_confidence`` / ``low_confidence_ids``, e.g. ``"508 of 512 subs
    solved (low-confidence: 3)"``).

    - ``session_id`` — an existing session of ``project``; unknown ->
      :class:`~seestar_controller.store.errors.SessionNotFoundError`;
    - ``call_store`` — see :func:`solve_session_unsolved` (None = direct);
    - ``min_stars`` / ``max_rms_arcsec`` — the low-confidence thresholds
      (see :func:`is_low_confidence_solve`; defaults
      :data:`LOW_CONFIDENCE_MIN_STARS` / :data:`LOW_CONFIDENCE_MAX_RMS_ARCSEC`).
    """
    call = call_store if call_store is not None else _direct_call
    call(lambda: _require_session(project, session_id))
    frames = call(lambda: list_frames(project, session_id=session_id, kind=KIND_SUB))
    sub_ids = {f.id for f in frames}
    solved_rows = [
        w
        for w in call(lambda: list_solved_frames(project, session_id))
        if w.frame_id in sub_ids
    ]
    low_confidence_ids = tuple(
        w.frame_id
        for w in solved_rows
        if is_low_confidence_solve(
            w.n_stars, w.rms_arcsec, min_stars=min_stars, max_rms_arcsec=max_rms_arcsec
        )
    )
    return SessionWcsCoverage(
        session_id=session_id,
        solved=len(solved_rows),
        total=len(frames),
        low_confidence=len(low_confidence_ids),
        low_confidence_ids=low_confidence_ids,
    )
