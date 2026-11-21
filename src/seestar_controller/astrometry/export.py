"""Annotated PNG export of a stored stack — the PURE export layer of
astrometry ticket 03 (:mod:`seestar_controller.astrometry.export`).

Pure-Python (module boundary per ``.scratch/astrometry/spec.md``): no GTK
imports, no :mod:`seestar_controller.link` / ``ui`` / ``enhance`` /
``photometry`` imports — the pure :mod:`seestar_controller.store` + the
sibling pure modules only. astropy enters lazily inside
:func:`_load_frame_image` (the export reads the stored FITS directly —
NOT :func:`~.solve.read_fits_bytes`, which squeezes and fails on RGB
stacks), so ``import seestar_controller.astrometry`` stays astropy-free.

What lives here:

- :func:`annotated_stack_png` — render the stored frame ``frame_id`` of
  ``project`` as an annotated PNG (the :mod:`.annotate` render: star labels
  + Gaia G mags from the persisted cross-match, the FOV rectangle with
  corner sky coordinates, and the scale bar, over the tone-mapped stack)
  and return the PNG file bytes;
- :func:`export_annotated_png` — the same, written atomically to ``path``
  (temp file + ``os.replace``), returning the :class:`~pathlib.Path`;
- :func:`default_export_name` — the default file name for a frame's
  annotated export (``annotated-<utc-stamp>.png``).

An unknown frame id, an unreadable FITS file, or a failed write raises
:class:`~.errors.ExportError`.
"""

from __future__ import annotations

import io
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from seestar_controller.store.crossmatch import load_crossmatch
from seestar_controller.store.frames import (
    Frame,
    header_to_dict,
    list_frames,
    read_frame_bytes,
)
from seestar_controller.store.project import Project

from .annotate import DEFAULT_MAX_OVERLAY_SIZE, annotated_png_bytes
from .errors import ExportError

#: The default long-side cap of the exported annotated PNG (the same default
#: as the gaia overlay render).
DEFAULT_EXPORT_MAX_SIZE = DEFAULT_MAX_OVERLAY_SIZE


def _resolve_frame(project: Project, frame_id: Any) -> Frame:
    """The stored frame with row id ``frame_id``; raises :class:`ExportError`
    naming the id when the project has no such frame."""
    if isinstance(frame_id, bool) or not isinstance(frame_id, int):
        raise ExportError(f"frame_id must be an int, got {frame_id!r}")
    for frame in list_frames(project):
        if frame.id == frame_id:
            return frame
    raise ExportError(
        f"project {project.slug!r} has no frame with id {frame_id!r} "
        f"(listed: {[f.id for f in list_frames(project)] or 'none'})"
    )


def _load_frame_image(project: Project, frame: Frame) -> tuple[np.ndarray, dict[str, Any]]:
    """Read the stored frame's 2-D float32 pixel array + sanitized header
    dict. astropy enters lazily here; an RGB stack (``NAXIS=3``) is converted
    to luminance (channel mean), like the gaia overlay's ``_gray``."""
    from astropy.io import fits as pyfits

    raw = read_frame_bytes(project, frame)
    try:
        with pyfits.open(io.BytesIO(raw)) as hdul:
            data = hdul[0].data
            header = header_to_dict(hdul[0].header)
    except Exception as exc:
        raise ExportError(
            f"cannot read the FITS of frame {frame.id!r} ({frame.path}): {exc}"
        ) from exc
    if data is None:
        raise ExportError(f"frame {frame.id!r} ({frame.path}) has no image data")
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim == 3:
        arr = arr.mean(axis=2)
    if arr.ndim != 2:
        raise ExportError(
            f"frame {frame.id!r} image must be 2-D (or RGB), got shape {arr.shape}"
        )
    return arr, header


def annotated_stack_png(
    project: Project,
    frame_id: int,
    *,
    max_size: int = DEFAULT_EXPORT_MAX_SIZE,
) -> bytes:
    """Render the stored frame ``frame_id`` of ``project`` as an annotated
    PNG and return the PNG file bytes: the tone-mapped stack with the
    persisted cross-match rows drawn on (matched stars ringed + labelled
    with their Gaia G magnitude, unmatched flagged), the FOV rectangle with
    corner sky coordinates (when the header has a celestial WCS), and the
    scale bar (when the header has a pixel scale).

    Raises :class:`ExportError` for an unknown frame id, an unreadable FITS,
    or a bad ``max_size``.
    """
    frame = _resolve_frame(project, frame_id)
    data, header = _load_frame_image(project, frame)
    rows = load_crossmatch(project, frame_id)
    return annotated_png_bytes(data, rows, header=header, max_size=max_size)


def export_annotated_png(
    project: Project,
    frame_id: int,
    path: str | Path,
    *,
    max_size: int = DEFAULT_EXPORT_MAX_SIZE,
) -> Path:
    """Render the annotated PNG of frame ``frame_id`` (see
    :func:`annotated_stack_png`) and write it ATOMICALLY to ``path`` (temp
    file in the same directory + ``os.replace``); returns the
    :class:`~pathlib.Path`. Raises :class:`ExportError` on any failure —
    a failed export never leaves a partial file behind."""
    out = Path(path)
    png = annotated_stack_png(project, frame_id, max_size=max_size)
    parent = out.parent if str(out.parent) else Path(".")
    try:
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{out.name}.", suffix=".tmp", dir=str(parent)
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(png)
            os.replace(tmp_name, out)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except ExportError:
        raise
    except Exception as exc:
        raise ExportError(f"cannot write the annotated PNG to {out}: {exc}") from exc
    return out


def default_export_name(frame: Frame) -> str:
    """The default export file name for ``frame``: ``annotated-<utc
    stamp>.png``, e.g. ``annotated-20261120T220010.png`` (the frame's UTC
    observation time with the separators stripped; ``annotated-stack.png``
    when the frame has no UTC)."""
    utc = frame.utc or ""
    stamp = re.sub(r"[^0-9A-Za-z]+", "", utc)[:15]
    return f"annotated-{stamp or 'stack'}.png"


__all__ = [
    "DEFAULT_EXPORT_MAX_SIZE",
    "annotated_stack_png",
    "default_export_name",
    "export_annotated_png",
]
