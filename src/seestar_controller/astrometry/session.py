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
  (``solved`` / ``total`` subs) as a frozen :class:`SessionWcsCoverage`, for
  the "508 of 512 frames solved" display.

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
    def attempted(self) -> int:
        """The number of unsolved sub-frames attempted (solved + skipped)."""
        return len(self.solved) + len(self.skipped)

    @property
    def summary(self) -> str:
        """A human-readable one-line batch summary: ``"4 of 5 unsolved subs
        solved (1 skipped)"``, or ``"no unsolved subs in session '...'"``."""
        attempted = self.attempted
        if attempted == 0:
            return f"no unsolved subs in session {self.session_id!r}"
        text = f"{len(self.solved)} of {attempted} unsolved subs solved"
        if self.skipped:
            text += f" ({len(self.skipped)} skipped)"
        return text


@dataclass(frozen=True)
class SessionWcsCoverage:
    """The WCS coverage of one session's sub-frames (issue 10): how many subs
    have a stored ``frame_wcs`` row.

    - ``session_id`` — the session;
    - ``solved`` — the subs of the session with a stored WCS record;
    - ``total`` — all sub-frames of the session (``kind="sub"``).
    """

    session_id: str
    solved: int
    total: int

    @property
    def unsolved(self) -> int:
        """The subs of the session without a stored WCS record."""
        return max(self.total - self.solved, 0)

    @property
    def fraction(self) -> float:
        """The solved fraction (0.0 when the session has no subs)."""
        return (self.solved / self.total) if self.total else 0.0

    @property
    def summary(self) -> str:
        """A human-readable one-line coverage: ``"4 of 512 subs solved"``,
        or ``"no subs in the session"``."""
        if self.total == 0:
            return "no subs in the session"
        return f"{self.solved} of {self.total} subs solved"


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
) -> SessionWcsCoverage:
    """The WCS coverage of one session's sub-frames (issue 10): how many of
    the session's ``kind="sub"`` frames have a stored ``frame_wcs`` record.
    Returns the frozen :class:`SessionWcsCoverage` (``solved`` / ``total``,
    e.g. ``"508 of 512 subs solved"``).

    - ``session_id`` — an existing session of ``project``; unknown ->
      :class:`~seestar_controller.store.errors.SessionNotFoundError`;
    - ``call_store`` — see :func:`solve_session_unsolved` (None = direct).
    """
    call = call_store if call_store is not None else _direct_call
    call(lambda: _require_session(project, session_id))
    frames = call(lambda: list_frames(project, session_id=session_id, kind=KIND_SUB))
    sub_ids = {f.id for f in frames}
    solved = sum(
        1
        for w in call(lambda: list_solved_frames(project, session_id))
        if w.frame_id in sub_ids
    )
    return SessionWcsCoverage(session_id=session_id, solved=solved, total=len(frames))
