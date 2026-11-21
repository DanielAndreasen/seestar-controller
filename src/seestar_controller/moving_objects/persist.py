"""Persist linked tracks / mover reports per session — FILE-BASED.

Pure-Python (module boundary per ``.scratch/moving-objects/spec.md``): no
GTK imports, no :mod:`seestar_controller.link` imports — a subprocess test
enforces the boundary. Stdlib only + the pure :mod:`.detect` / :mod:`.link`
/ :mod:`.flag` and the store's path helpers.

What lives here:

- :data:`MOVERS_FILE` — the file name (``movers.json``) written under the
  session's subs directory: ``<project>/subs/<session_id>/movers.json``;
- :func:`tracks_path` — resolve that path (and validate ``session_id``);
- :func:`save_tracks` — atomically write the tracks + mover reports of a
  session as JSON (temp file in the same directory + ``fsync`` +
  :func:`os.replace`, the same pattern as the store's frame writes);
- :func:`load_tracks` — read them back into a :class:`SessionTracks`.

The store schema is NOT touched: this is a plain JSON sidecar file next to
the session's subs, so existing projects keep working unchanged.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from seestar_controller.store.errors import SessionNotFoundError
from seestar_controller.store.project import SUBS_DIR, utc_now_iso

from .detect import Detection
from .errors import PersistenceError
from .flag import MoverReport
from .link import Track
from .mpc import MpcMatch

#: The sidecar file name under ``<project>/subs/<session_id>/``.
MOVERS_FILE = "movers.json"

#: Schema version of the sidecar JSON payload. Version 2 adds the optional
#: ``mpc_match`` field on each mover entry (the MPC lookup result, issue 02).
SCHEMA_VERSION = 2

#: Schema versions accepted on load: version 1 files (no ``mpc_match``) are
#: migrated on read — their movers get ``mpc_match=None``.
SUPPORTED_SCHEMA_VERSIONS = (1, 2)

#: session ids are directory names — keep them slug-safe.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class SessionTracks:
    """The persisted tracks + mover reports of one session."""

    session_id: str
    tracks: tuple[Track, ...] = ()
    movers: tuple[MoverReport, ...] = ()
    saved_at: str | None = None


def _validate_session_id(session_id: Any) -> str:
    if (
        not isinstance(session_id, str)
        or not _SESSION_ID_RE.match(session_id)
        or session_id in {".", ".."}
    ):
        raise PersistenceError(f"invalid session_id {session_id!r}")
    return session_id


def _check_session(project: Any, session_id: str) -> None:
    row = project.db.execute(
        "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise SessionNotFoundError(f"unknown session {session_id!r}")


def tracks_path(project: Any, session_id: str) -> Path:
    """The sidecar path ``<project>/subs/<session_id>/movers.json``.

    Validates ``session_id`` (it becomes a directory name) — raises
    :class:`~.errors.PersistenceError` on an unsafe value.
    """
    sid = _validate_session_id(session_id)
    return Path(project.path) / SUBS_DIR / sid / MOVERS_FILE


def _detection_from_dict(d: Any) -> Detection:
    if not isinstance(d, dict):
        raise PersistenceError(f"detection entry is not an object: {d!r}")
    try:
        return Detection(
            frame_id=int(d["frame_id"]),
            path=str(d["path"]),
            jd=float(d["jd"]),
            utc=str(d["utc"]),
            x=float(d["x"]),
            y=float(d["y"]),
            peak=float(d["peak"]),
            flux=float(d["flux"]),
            ra_deg=None if d.get("ra_deg") is None else float(d["ra_deg"]),
            dec_deg=None if d.get("dec_deg") is None else float(d["dec_deg"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PersistenceError(f"corrupt detection entry {d!r}: {exc}") from exc


def _track_from_dict(d: Any) -> Track:
    if not isinstance(d, dict):
        raise PersistenceError(f"track entry is not an object: {d!r}")
    try:
        points = tuple(_detection_from_dict(p) for p in d["points"])
    except (KeyError, TypeError) as exc:
        raise PersistenceError(f"corrupt track entry {d!r}: {exc}") from exc
    if not points:
        raise PersistenceError(f"track entry has no points: {d!r}")
    try:
        track_id = int(d["track_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PersistenceError(f"corrupt track entry {d!r}: {exc}") from exc
    return Track(track_id=track_id, points=points)


_MOVER_FIELDS = (
    "track_id",
    "n_frames",
    "start_utc",
    "end_utc",
    "start_jd",
    "end_jd",
    "start_x",
    "start_y",
    "end_x",
    "end_y",
    "motion_px",
    "speed_px_per_frame",
    "direction_deg",
    "fit_residual_px",
    "speed_arcsec_per_frame",
    "start_ra_deg",
    "start_dec_deg",
    "end_ra_deg",
    "end_dec_deg",
    "mpc_match",
)

#: The fields of a serialized :class:`~.mpc.MpcMatch` (``mpc_match``).
_MPC_MATCH_FIELDS = frozenset(
    {"designation", "name", "offset_arcsec", "motion_arcsec_per_min", "pa_deg", "v_mag"}
)


def _mpc_match_from_dict(d: Any) -> MpcMatch:
    if not isinstance(d, dict) or not _MPC_MATCH_FIELDS.issubset(d):
        raise PersistenceError(f"mpc_match entry is not an object: {d!r}")
    try:
        return MpcMatch(
            designation=str(d["designation"]),
            name=None if d.get("name") is None else str(d["name"]),
            offset_arcsec=float(d["offset_arcsec"]),
            motion_arcsec_per_min=float(d["motion_arcsec_per_min"]),
            pa_deg=float(d["pa_deg"]),
            v_mag=None if d.get("v_mag") is None else float(d["v_mag"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PersistenceError(f"corrupt mpc_match entry {d!r}: {exc}") from exc


def _mover_from_dict(d: Any) -> MoverReport:
    if not isinstance(d, dict):
        raise PersistenceError(f"mover entry is not an object: {d!r}")
    try:
        values: dict[str, Any] = {}
        for name in _MOVER_FIELDS:
            v = d.get(name) if name == "mpc_match" else d[name]
            if name == "mpc_match":
                # schema_version 1 files have no mpc_match -> None (migrated).
                values[name] = None if v is None else _mpc_match_from_dict(v)
            elif name in {"track_id", "n_frames"}:
                values[name] = int(v)
            elif name in {"start_utc", "end_utc"}:
                values[name] = str(v)
            elif v is None:
                values[name] = None
            else:
                values[name] = float(v)
        return MoverReport(**values)
    except (KeyError, TypeError, ValueError) as exc:
        raise PersistenceError(f"corrupt mover entry {d!r}: {exc}") from exc


def save_tracks(
    project: Any,
    session_id: str,
    tracks: list[Track] | tuple[Track, ...],
    movers: list[MoverReport] | tuple[MoverReport, ...] = (),
    *,
    saved_at: str | None = None,
) -> Path:
    """Atomically write the tracks + mover reports of a session to
    ``<project>/subs/<session_id>/movers.json`` and return the path.

    The session must exist in the store (raises
    :class:`~seestar_controller.store.errors.SessionNotFoundError`
    otherwise). The write is atomic: a temp file in the same directory is
    flushed + fsynced, then :func:`os.replace`d over the target (removed on
    failure) — the same pattern as the store's frame writes.
    """
    sid = _validate_session_id(session_id)
    _check_session(project, sid)
    for track in tracks:
        if not isinstance(track, Track):
            raise PersistenceError(f"tracks must be Track records, got {type(track).__name__}")
    for mover in movers:
        if not isinstance(mover, MoverReport):
            raise PersistenceError(
                f"movers must be MoverReport records, got {type(mover).__name__}"
            )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "session_id": sid,
        "saved_at": saved_at if saved_at is not None else utc_now_iso(),
        "tracks": [
            {"track_id": t.track_id, "points": [asdict(p) for p in t.points]}
            for t in tracks
        ],
        "movers": [asdict(m) for m in movers],
    }
    path = tracks_path(project, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=MOVERS_FILE + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def load_tracks(project: Any, session_id: str) -> SessionTracks:
    """Read the persisted tracks + mover reports of a session.

    Raises :class:`~.errors.PersistenceError` when the sidecar file is
    missing or corrupt, and
    :class:`~seestar_controller.store.errors.SessionNotFoundError` when the
    session does not exist in the store.
    """
    sid = _validate_session_id(session_id)
    _check_session(project, sid)
    path = tracks_path(project, sid)
    if not path.is_file():
        raise PersistenceError(f"no movers file for session {sid!r}: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PersistenceError(f"cannot read movers file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PersistenceError(f"corrupt movers file {path}: not a JSON object")
    version = payload.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise PersistenceError(
            f"unsupported movers schema_version {version!r} in {path} "
            f"(supported: {SUPPORTED_SCHEMA_VERSIONS})"
        )
    if payload.get("session_id") != sid:
        raise PersistenceError(
            f"movers file {path} belongs to session {payload.get('session_id')!r}, "
            f"not {sid!r}"
        )
    try:
        tracks = tuple(_track_from_dict(t) for t in payload.get("tracks", []))
        movers = tuple(_mover_from_dict(m) for m in payload.get("movers", []))
        saved_at = payload.get("saved_at")
    except PersistenceError:
        raise
    except (TypeError, ValueError) as exc:
        raise PersistenceError(f"corrupt movers file {path}: {exc}") from exc
    return SessionTracks(
        session_id=sid,
        tracks=tracks,
        movers=movers,
        saved_at=None if saved_at is None else str(saved_at),
    )


__all__ = [
    "MOVERS_FILE",
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "SessionTracks",
    "load_tracks",
    "save_tracks",
    "tracks_path",
]
