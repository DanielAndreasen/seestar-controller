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
  unknown frame -> :class:`~seestar_controller.store.errors.FrameStoreError`).

Typical flow::

    from seestar_controller.astrometry import solve_session_frame

    result = solve_session_frame(project, session_id, frame_id)
    result.center_ra_deg, result.center_dec_deg, result.rms_arcsec
"""

from __future__ import annotations

from typing import Any

from seestar_controller.store.crossmatch import save_crossmatch
from seestar_controller.store.errors import FrameStoreError, SessionNotFoundError
from seestar_controller.store.frames import (
    Frame,
    get_frame_path,
    list_frames,
    read_frame_bytes,
)
from seestar_controller.store.project import Project
from seestar_controller.store.wcs import save_frame_wcs

from .solve import SolveResult, read_fits_bytes, solve_frame
from .wcs import write_wcs_to_fits


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


def solve_project_frame(
    project: Project,
    frame: Frame | int,
    *,
    solver: str = "auto",
    write_header: bool = True,
    persist: bool = True,
    save_matches: bool = True,
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
      :func:`seestar_controller.store.crossmatch.save_crossmatch`.

    The solve runs on the frame's stored bytes; a frame that cannot be solved
    raises :class:`~.solve.SolveError` and NOTHING is written (neither the
    FITS header nor the db).
    """
    resolved = _resolve_frame(project, frame)
    data, header = read_fits_bytes(read_frame_bytes(project, resolved))
    result = solve_frame(data=data, header=header, solver=solver, **solve_kwargs)
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
