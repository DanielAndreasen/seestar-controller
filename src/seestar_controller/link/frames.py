"""Wire contract for frame downloads, shared by the facade and the mock Scope.

The Scope stores **sub-frames** (individual exposures, GLOSSARY.md) and
**Stacks** (the stacked FITS images) on its internal storage. On the wire
(JSON-RPC-style messages on the command channel :4700, seestar_alp /
seestarpy message shapes):

- list: ``list_frames`` with params ``{"kind": "sub"|"stack", "since": epoch}``
  (``since`` optional) -> result ``{"frames": [record, ...]}`` where each
  record is ``{"id", "kind", "time", "epoch", "exposure", "gain", "target",
  "size"}`` sorted by time;
- fetch: ``get_frame`` with params ``{"id": ...}`` -> the record plus
  ``fits_b64``, the FITS file's bytes base64-encoded for JSON transport.

The client side (:func:`download_frames`, exposed on the facade as
:meth:`~seestar_controller.link.device.Device.download_frames`) lists the
frames of one kind, fetches each one's FITS bytes, and writes them into a
caller-given directory as ``<id>.fits`` plus a ``<id>.json`` metadata
sidecar (time, exposure, gain, target). Both files are written **atomically**
(temp file in the same directory + fsync + :func:`os.replace`), so an
interrupted download never leaves a partial file behind.

NOTE (user AFK, no real scope — docs/wiki/assumptions.md): the method names
and record shapes are the mock Scope's contract, unverified against real
hardware.
"""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

#: Frame kind: a **sub-frame** (one individual exposure, GLOSSARY.md).
KIND_SUB = "sub"
#: Frame kind: a **Stack** (the stacked FITS image, GLOSSARY.md).
KIND_STACK = "stack"
#: The valid frame kinds.
KINDS = (KIND_SUB, KIND_STACK)

#: RPC that lists the frames stored on the Scope.
LIST_FRAMES_METHOD = "list_frames"
#: RPC that fetches one frame's FITS bytes (base64) by id.
GET_FRAME_METHOD = "get_frame"

#: Suffix of the FITS file written per downloaded frame.
FITS_SUFFIX = ".fits"
#: Suffix of the JSON metadata sidecar written next to each FITS file.
SIDECAR_SUFFIX = ".json"


def normalize_kind(kind: Any) -> str:
    """Validate a frame ``kind`` ("sub" or "stack"); raise ``ValueError``."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    return str(kind)


def iso_utc(when: datetime) -> str:
    """Format ``when`` as an ISO-8601 UTC string (FITS ``DATE-OBS`` style,
    no timezone suffix): ``YYYY-MM-DDTHH:MM:SS.mmm``."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", ""
    )


def normalize_since(since: Any) -> float | None:
    """Normalize a ``since`` filter to epoch seconds (UTC).

    Accepts None (no filter), a :class:`~datetime.datetime` (naive = UTC),
    an ISO-8601 string (``Z`` suffix allowed), or epoch seconds as
    int/float. Raises ``ValueError`` on anything else.
    """
    if since is None:
        return None
    if isinstance(since, datetime):
        when = since if since.tzinfo is not None else since.replace(tzinfo=timezone.utc)
        return when.timestamp()
    if isinstance(since, str):
        try:
            parsed = datetime.fromisoformat(since.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"since must be an ISO-8601 datetime string, got {since!r}") from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    if isinstance(since, bool):
        raise ValueError(f"since must be a datetime, ISO string, or epoch seconds, got {since!r}")
    if isinstance(since, (int, float)):
        return float(since)
    raise ValueError(
        f"since must be a datetime, ISO string, or epoch seconds, got {since!r}"
    )


@dataclass(frozen=True)
class FrameMeta:
    """Metadata of one frame stored on the Scope.

    ``time`` is the ISO-8601 UTC observation time (the FITS ``DATE-OBS``
    header), ``epoch`` the same as epoch seconds, ``exposure`` the sub-frame
    exposure in seconds, ``gain`` the sensor gain, and ``target`` the
    current target as ``{"ra": ..., "dec": ...}`` (hours/degrees) or None.
    """

    id: str
    kind: str
    time: str
    epoch: float
    exposure: float
    gain: int
    target: Mapping[str, Any] | None


def _normalize_target(raw: Any) -> Mapping[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        target: dict[str, Any] = {}
        if "ra" in raw:
            target["ra"] = float(raw["ra"])
        if "dec" in raw:
            target["dec"] = float(raw["dec"])
        if "name" in raw:
            target["name"] = str(raw["name"])
        return target or None
    raise ValueError(f"target must be a mapping or None, got {raw!r}")


def parse_frame_meta(raw: Mapping[str, Any]) -> FrameMeta:
    """Parse one wire frame record into a :class:`FrameMeta`.

    Raises ``ValueError``/``KeyError`` on malformed records.
    """
    if "id" not in raw:
        raise ValueError(f"frame record has no id: {dict(raw)!r}")
    return FrameMeta(
        id=str(raw["id"]),
        kind=normalize_kind(raw.get("kind")),
        time=str(raw.get("time", "")),
        epoch=float(raw.get("epoch", 0.0) or 0.0),
        exposure=float(raw.get("exposure", 0.0) or 0.0),
        gain=int(raw.get("gain", 0) or 0),
        target=_normalize_target(raw.get("target")),
    )


def meta_to_sidecar(meta: FrameMeta, *, size: int | None = None) -> dict[str, Any]:
    """Build the JSON sidecar dict for a downloaded frame (time, exposure,
    gain, target, plus id/kind/size and the download time)."""
    sidecar: dict[str, Any] = {
        "id": meta.id,
        "kind": meta.kind,
        "time": meta.time,
        "epoch": meta.epoch,
        "exposure": meta.exposure,
        "gain": meta.gain,
        "target": dict(meta.target) if meta.target is not None else None,
        "downloaded_at": iso_utc(datetime.now(timezone.utc)),
    }
    if size is not None:
        sidecar["size"] = int(size)
    return sidecar


@dataclass(frozen=True)
class DownloadedFrame:
    """One downloaded frame on local disk.

    ``path`` is the ``.fits`` file, ``sidecar`` the ``.json`` metadata file
    next to it, ``meta`` the frame's metadata.
    """

    path: Path
    sidecar: Path
    meta: FrameMeta


def encode_fits_b64(fits_bytes: bytes) -> str:
    """Encode FITS bytes for JSON transport (base64 ASCII)."""
    return base64.b64encode(fits_bytes).decode("ascii")


def decode_fits_b64(data: Any) -> bytes:
    """Decode base64 FITS bytes from the wire; ``ValueError`` on bad input."""
    if not isinstance(data, str):
        raise ValueError(f"fits_b64 must be a base64 string, got {type(data).__name__}")
    try:
        return base64.b64decode(data.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError(f"invalid base64 FITS payload: {exc}") from None


_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def safe_frame_name(frame_id: Any) -> str:
    """Sanitize a frame id into a safe file stem (no path separators)."""
    name = _UNSAFE_CHARS.sub("_", str(frame_id)).strip(".")
    if not name or name in (".", ".."):
        raise ValueError(f"frame id {frame_id!r} is not usable as a file name")
    return name


def atomic_write_bytes(path: str | Path, data: bytes) -> Path:
    """Write ``data`` to ``path`` atomically: temp file in the same directory,
    flush + fsync, then :func:`os.replace` onto the destination.

    On any failure (including :class:`KeyboardInterrupt`) the temp file is
    removed and the destination is left untouched — a reader never sees a
    partial file. Returns the destination path.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.close(fd)  # already closed when the with-block exited
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def atomic_write_json(path: str | Path, obj: Any) -> Path:
    """Write ``obj`` as pretty JSON to ``path`` atomically (see
    :func:`atomic_write_bytes`)."""
    data = (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return atomic_write_bytes(path, data)


def download_frames(
    transport: Any,
    dest_dir: str | Path,
    kind: str = KIND_SUB,
    since: Any = None,
    *,
    timeout: float | None = None,
) -> list[DownloadedFrame]:
    """Download frames of one ``kind`` ("sub"/"stack") from the Scope into
    ``dest_dir`` via the transport's command channel.

    Lists the Scope's frames (``list_frames``), optionally filtered to
    ``since`` (datetime / ISO string / epoch seconds), fetches each frame's
    FITS bytes (``get_frame``), and writes ``<id>.fits`` + ``<id>.json``
    sidecar into ``dest_dir`` — both atomically. Returns the
    :class:`DownloadedFrame` list (path, sidecar, metadata) in time order.
    """
    kind = normalize_kind(kind)
    since_epoch = normalize_since(since)
    dest = Path(dest_dir)
    params: dict[str, Any] = {"kind": kind}
    if since_epoch is not None:
        params["since"] = since_epoch
    result = transport.request(LIST_FRAMES_METHOD, params, timeout=timeout)
    records = result.get("frames", []) if isinstance(result, Mapping) else []
    dest.mkdir(parents=True, exist_ok=True)
    downloaded: list[DownloadedFrame] = []
    for raw in records:
        meta = parse_frame_meta(raw)
        if since_epoch is not None and meta.epoch < since_epoch:
            continue  # belt and braces: the Scope already filtered
        detail = transport.request(GET_FRAME_METHOD, {"id": meta.id}, timeout=timeout)
        fits_bytes = decode_fits_b64(detail.get("fits_b64"))
        expected_size = detail.get("size")
        if expected_size is not None and int(expected_size) != len(fits_bytes):
            raise ValueError(
                f"frame {meta.id}: FITS payload is {len(fits_bytes)} bytes, "
                f"expected {expected_size}"
            )
        name = safe_frame_name(meta.id)
        fits_path = dest / (name + FITS_SUFFIX)
        sidecar_path = dest / (name + SIDECAR_SUFFIX)
        atomic_write_bytes(fits_path, fits_bytes)
        atomic_write_json(
            sidecar_path, meta_to_sidecar(meta, size=len(fits_bytes))
        )
        downloaded.append(
            DownloadedFrame(path=fits_path, sidecar=sidecar_path, meta=meta)
        )
    return downloaded
