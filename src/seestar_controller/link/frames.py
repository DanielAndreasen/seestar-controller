"""Wire contract for frame downloads, shared by the facade and the mock Scope.

The Scope stores **sub-frames** (individual exposures, GLOSSARY.md) and
**Stacks** (the stacked FITS images) on its internal storage under
``MyWorks/``. Two wire dialects exist (live-probed against the Seestar S50,
station mode, fw 9.75, 2026-11-20 — see :mod:`.compat`):

- **fw < 9.75** (JSON-RPC on the command channel :4700):
  - list: ``list_frames`` with params ``{"kind": "sub"|"stack", "since": epoch}``
    (``since`` optional) -> result ``{"frames": [record, ...]}`` where each
    record is ``{"id", "kind", "time", "epoch", "exposure", "gain", "target",
    "size"}`` sorted by time;
  - fetch: ``get_frame`` with params ``{"id": ...}`` -> the record plus
    ``fits_b64``, the FITS file's bytes base64-encoded for JSON transport.
- **fw 9.75** (``list_frames``/``get_frame`` answer code 103):
  - list: ``get_albums`` (no params) -> ``{"path": "MyWorks", "list":
    [{group_name, files: [{name, thn, count, type}]}]}`` where ``files[].name``
    are folders directly under ``MyWorks`` (sub-frame folders end in ``_sub``,
    stack folders are plain names). Per folder:
    ``get_img_file_page_number`` with ``{"dir": "MyWorks/<folder>",
    "skip_avi": false}`` -> int page count (20 files per page; unknown dir ->
    0), then ``get_img_file_page_name`` with ``{"page": int}`` (stateful
    cursor set by the page-number call) -> list of ``{name, is_dir, date,
    size_k, header_date}``; page out of range -> code 204;
  - fetch: plain HTTP GET ``http://<ip>/<url-quoted MyWorks/... path>``
    (e.g. ``/MyWorks/NGC%206888_sub/Light_NGC%206888_10.0s_LP_20250824-001334.fit``
    -> 200 ``application/octet-stream``). Filenames look like
    ``Light_<target>_<exp>s_<filter>_<yyyymmdd>-<hhmmss>.fit`` and
    ``Stacked_<n>_...`` / ``DSO_Stacked_...``; the FITS header carries
    DATE-OBS / EXPTIME / GAIN / OBJECT / RA / DEC.

The client side (:func:`download_frames`, exposed on the facade as
:meth:`~seestar_controller.link.device.Device.download_frames`) lists the
frames of one kind, fetches each one's FITS bytes, and writes them into a
caller-given directory as ``<id>.fits`` plus a ``<id>.json`` metadata
sidecar (time, exposure, gain, target). Both files are written **atomically**
(temp file in the same directory + fsync + :func:`os.replace`), so an
interrupted download never leaves a partial file behind.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from . import compat
from .transport import RpcError

#: Frame kind: a **sub-frame** (one individual exposure, GLOSSARY.md).
KIND_SUB = "sub"
#: Frame kind: a **Stack** (the stacked FITS image, GLOSSARY.md).
KIND_STACK = "stack"
#: The valid frame kinds.
KINDS = (KIND_SUB, KIND_STACK)

#: RPC that lists the frames stored on the Scope (fw < 9.75; fw 9.75 -> 103).
LIST_FRAMES_METHOD = "list_frames"
#: RPC that fetches one frame's FITS bytes (base64) by id (fw < 9.75; 9.75 -> 103).
GET_FRAME_METHOD = "get_frame"

#: fw 9.75 album root folder on the Scope's storage.
ROOT_DIR = "MyWorks"
#: fw 9.75 folder suffix marking sub-frame folders (stack folders are plain).
SUB_FOLDER_SUFFIX = "_sub"
#: fw 9.75 files per page in ``get_img_file_page_name`` listings.
PAGE_SIZE = 20
#: FITS file extensions as they appear in album listings / remote paths.
FITS_FILE_SUFFIXES = (".fit", ".fits")

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


# -- fw 9.75 album listing + HTTP fetch ------------------------------------------


def http_frame_url(host: str, remote_path: str) -> str:
    """HTTP GET URL for a frame stored on the Scope (fw 9.75).

    ``remote_path`` is the album path relative to the Scope's web root,
    e.g. ``MyWorks/NGC 6888_sub/Light_..._20250824-001334.fit``; it is
    URL-quoted with ``/`` kept literal.
    """
    return f"http://{host}/{urllib.parse.quote(remote_path, safe='/')}"


def default_fetch(url: str, timeout: float = 30.0) -> bytes:
    """Default HTTP GET for fw 9.75 frame downloads (``urllib``)."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def _parse_iso_epoch(value: Any) -> float | None:
    """Parse an ISO-8601 timestamp (naive = UTC) to epoch seconds, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _listing_epoch(entry: Mapping[str, Any]) -> float | None:
    """Epoch seconds from a page-listing entry (``header_date`` ISO, else
    ``date`` "YYYY-MM-DD HH:MM:SS", both UTC)."""
    return _parse_iso_epoch(entry.get("header_date")) or _parse_iso_epoch(
        str(entry.get("date", "")).replace(" ", "T")
    )


def _album_folders(albums: Any, kind: str) -> list[str]:
    """Folder names (directly under ``MyWorks``) holding frames of ``kind``."""
    folders: set[str] = set()
    groups = albums.get("list", []) if isinstance(albums, Mapping) else []
    for group in groups or []:
        if not isinstance(group, Mapping):
            continue
        for entry in group.get("files", []) or []:
            if not isinstance(entry, Mapping):
                continue
            name = str(entry.get("name", ""))
            if not name:
                continue
            if kind == KIND_SUB and not name.endswith(SUB_FOLDER_SUFFIX):
                continue
            if kind == KIND_STACK and name.endswith(SUB_FOLDER_SUFFIX):
                continue
            folders.add(name)
    return sorted(folders)


def _records_from_albums(
    transport: Any,
    albums: Any,
    kind: str,
    since_epoch: float | None,
    *,
    timeout: float | None = None,
) -> list[dict[str, Any]]:
    """Normalize a fw 9.75 ``get_albums`` result into frame records.

    Pages through every folder of the requested kind
    (``get_img_file_page_number`` + ``get_img_file_page_name``) and returns
    records shaped like the legacy ``list_frames`` records plus
    ``remote_path`` (for the HTTP download) and ``size`` (bytes, from the
    listing's ``size_k``).
    """
    records: list[dict[str, Any]] = []
    for folder in _album_folders(albums, kind):
        dir_path = f"{ROOT_DIR}/{folder}"
        pages = transport.request(
            compat.PAGE_NUMBER_METHOD,
            {"dir": dir_path, "skip_avi": False},
            timeout=timeout,
        )
        try:
            page_count = int(pages)
        except (TypeError, ValueError):
            page_count = 0
        for page in range(max(0, page_count)):
            try:
                entries = transport.request(
                    compat.PAGE_NAME_METHOD, {"page": page}, timeout=timeout
                )
            except RpcError as exc:
                if exc.reply.code == compat.OUT_OF_LIMIT_CODE:
                    break  # folder shrank between the two calls
                raise
            for entry in entries or []:
                if not isinstance(entry, Mapping) or entry.get("is_dir"):
                    continue
                name = str(entry.get("name", ""))
                if not name.lower().endswith(FITS_FILE_SUFFIXES):
                    continue
                epoch = _listing_epoch(entry)
                if since_epoch is not None and epoch is not None and epoch < since_epoch:
                    continue
                records.append(
                    {
                        "id": f"{folder}/{name}",
                        "kind": kind,
                        "time": str(entry.get("header_date") or entry.get("date") or ""),
                        "epoch": epoch if epoch is not None else 0.0,
                        "size": int(entry.get("size_k", 0) or 0) * 1024,
                        "remote_path": f"{dir_path}/{name}",
                    }
                )
    records.sort(key=lambda r: (r["epoch"], r["id"]))
    return records


def list_remote_frames(
    transport: Any,
    kind: str,
    since_epoch: float | None = None,
    *,
    timeout: float | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """List the Scope's frames of one ``kind``; returns ``(method, records)``.

    fw 9.75: ``get_albums`` + paged folder listing (records carry
    ``remote_path`` for the HTTP download). fw < 9.75: ``list_frames``.
    The working method is chosen by the :mod:`.compat` shim and cached on
    the transport.
    """
    kind = normalize_kind(kind)

    def params_for(method: str) -> Mapping[str, Any] | None:
        if method == compat.GET_ALBUMS_METHOD:
            return None
        params: dict[str, Any] = {"kind": kind}
        if since_epoch is not None:
            params["since"] = since_epoch
        return params

    method, result = compat.call(
        transport, compat.FRAMES_LIST_METHODS, params_for, timeout=timeout
    )
    if method == compat.GET_ALBUMS_METHOD:
        records = _records_from_albums(transport, result, kind, since_epoch, timeout=timeout)
    else:
        records = [
            dict(raw)
            for raw in (result.get("frames", []) if isinstance(result, Mapping) else [])
        ]
    return method, records


def fetch_frame_bytes(
    transport: Any,
    record: Mapping[str, Any],
    *,
    fetch: Callable[[str], bytes] | None = None,
    timeout: float | None = None,
) -> bytes:
    """Fetch one frame's FITS bytes.

    Records with ``remote_path`` (fw 9.75 album flow) are downloaded over
    plain HTTP GET from the Scope (``fetch(url) -> bytes``, default
    :func:`default_fetch`); legacy records go through the ``get_frame``
    RPC (``fits_b64``).
    """
    remote_path = record.get("remote_path")
    if remote_path:
        host = getattr(transport, "host", "")
        url = http_frame_url(host, str(remote_path))
        fetcher = fetch or default_fetch
        return fetcher(url)
    detail = transport.request(
        GET_FRAME_METHOD, {"id": record["id"]}, timeout=timeout
    )
    return decode_fits_b64(detail.get("fits_b64"))


_FILENAME_RE = re.compile(
    r"^(?:Light|Stacked_\d+|DSO_Stacked)_(?P<target>.+?)_(?P<exp>\d+(?:\.\d+)?)s"
    r"(?:_(?P<filter>[A-Za-z0-9]+))?_(?P<date>\d{8})-(?P<tod>\d{6})\.fits?$"
)


def parse_filename_meta(name: str) -> dict[str, Any]:
    """Best-effort metadata from a fw 9.75 frame filename
    (``Light_<target>_<exp>s_<filter>_<yyyymmdd>-<hhmmss>.fit``).

    Returns ``{"exposure": float, "target": {"name": str}, "time": iso,
    "epoch": float}`` for the parts that parse, else ``{}``.
    """
    match = _FILENAME_RE.match(str(name))
    if not match:
        return {}
    meta: dict[str, Any] = {
        "exposure": float(match.group("exp")),
        "target": {"name": match.group("target")},
    }
    try:
        when = datetime.strptime(
            match.group("date") + match.group("tod"), "%Y%m%d%H%M%S"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return meta
    meta["time"] = iso_utc(when)
    meta["epoch"] = when.timestamp()
    return meta


def read_fits_header_meta(fits_bytes: bytes) -> dict[str, Any]:
    """Best-effort FITS primary-header read (astropy, lazily imported):
    DATE-OBS / EXPTIME / GAIN / OBJECT / RA / DEC.

    Returns ``{"time", "exposure", "gain", "target": {"name"?, "ra"?,
    "dec"?}}`` for the headers present; ``{}`` when astropy is unavailable
    or the bytes are not a readable FITS file.
    """
    try:
        from astropy.io import fits as pyfits
    except Exception:
        return {}
    try:
        with pyfits.open(io.BytesIO(fits_bytes), memmap=False) as hdul:
            header = hdul[0].header
            meta: dict[str, Any] = {}
            if "DATE-OBS" in header:
                meta["time"] = str(header["DATE-OBS"]).strip()
            if "EXPTIME" in header:
                meta["exposure"] = float(header["EXPTIME"])
            if "GAIN" in header:
                meta["gain"] = int(header["GAIN"])
            target: dict[str, Any] = {}
            if "OBJECT" in header:
                target["name"] = str(header["OBJECT"]).strip()
            if "RA" in header:
                target["ra"] = float(header["RA"])
            if "DEC" in header:
                target["dec"] = float(header["DEC"])
            if target:
                meta["target"] = target
            return meta
    except Exception:
        return {}


def _meta_from_remote_record(
    record: Mapping[str, Any], fits_bytes: bytes
) -> FrameMeta:
    """Build a :class:`FrameMeta` for a fw 9.75 album record, merging the
    FITS header (preferred), the listing entry, and the filename."""
    header = read_fits_header_meta(fits_bytes)
    filename = parse_filename_meta(str(record["id"]).rsplit("/", 1)[-1])
    time_str = (
        header.get("time") or record.get("time") or filename.get("time") or ""
    )
    epoch = (
        _parse_iso_epoch(time_str)
        or float(record.get("epoch", 0.0) or 0.0)
        or filename.get("epoch", 0.0)
    )
    exposure = header.get("exposure", filename.get("exposure", 0.0))
    gain = header.get("gain", 0)
    target = header.get("target") or filename.get("target")
    return FrameMeta(
        id=str(record["id"]),
        kind=normalize_kind(record.get("kind")),
        time=str(time_str),
        epoch=float(epoch),
        exposure=float(exposure),
        gain=int(gain),
        target=_normalize_target(target),
    )


def _local_stem(frame_id: Any) -> str:
    """Frame id without its original FITS extension (for the local file name)."""
    stem = str(frame_id)
    lower = stem.lower()
    for suffix in FITS_FILE_SUFFIXES:
        if lower.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def download_frames(
    transport: Any,
    dest_dir: str | Path,
    kind: str = KIND_SUB,
    since: Any = None,
    *,
    timeout: float | None = None,
    fetch: Callable[[str], bytes] | None = None,
) -> list[DownloadedFrame]:
    """Download frames of one ``kind`` ("sub"/"stack") from the Scope into
    ``dest_dir`` via the transport's command channel.

    Lists the Scope's frames — ``get_albums`` + paged listing + HTTP GET on
    fw 9.75, ``list_frames`` + ``get_frame`` on older firmware (chosen by
    the :mod:`.compat` shim) — optionally filtered to ``since`` (datetime /
    ISO string / epoch seconds), and writes ``<id>.fits`` + ``<id>.json``
    sidecar into ``dest_dir``, both atomically. Returns the
    :class:`DownloadedFrame` list (path, sidecar, metadata) in time order.

    ``fetch`` injects the fw 9.75 HTTP GET (``fetch(url) -> bytes``);
    default :func:`default_fetch`.
    """
    kind = normalize_kind(kind)
    since_epoch = normalize_since(since)
    dest = Path(dest_dir)
    _method, records = list_remote_frames(transport, kind, since_epoch, timeout=timeout)
    dest.mkdir(parents=True, exist_ok=True)
    downloaded: list[DownloadedFrame] = []
    for raw in records:
        if raw.get("remote_path"):
            # fw 9.75 album flow: HTTP download, metadata from the FITS header
            fits_bytes = fetch_frame_bytes(transport, raw, fetch=fetch, timeout=timeout)
            expected_size = raw.get("size")
            if expected_size and abs(int(expected_size) - len(fits_bytes)) > 2048:
                raise ValueError(
                    f"frame {raw['id']}: FITS payload is {len(fits_bytes)} bytes, "
                    f"listing said ~{expected_size}"
                )
            meta = _meta_from_remote_record(raw, fits_bytes)
        else:
            # legacy flow: get_frame RPC with fits_b64
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
        name = safe_frame_name(_local_stem(meta.id))
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
