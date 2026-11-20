"""Save / export / presets — the PURE persistence layer of
:mod:`seestar_controller.enhance` (image-enhance ticket 04).

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) — stdlib +
:mod:`numpy` + :mod:`PIL` + :mod:`seestar_controller.store` + the sibling pure
modules only. The one heavy third-party import, :mod:`astropy.io.fits`, is
lazy inside :func:`save_enhanced` — the same pattern as
:mod:`seestar_controller.store.frames` and :mod:`.fits_io`.

What lives here:

- :func:`save_enhanced` — render the current pipeline at FULL resolution and
  store the result as a new ``enhanced`` frame in the project (the original
  stack FITS is never touched — verified by hash in the tests): the pipeline
  output is written as a float32 FITS file (header copied from the source
  image, structural cards skipped, a ``HISTORY`` card added) via
  :func:`seestar_controller.store.frames.store_frame` with
  ``kind="enhanced"`` (``enhanced/<session>/<jd>.fits``);
- :func:`export_render` — export a rendered image (a PIL image, a uint8
  array, or a float array — tone-mapped first) as PNG or JPEG via PIL, with
  format/extension validation and an atomic write;
- :func:`save_preset_for` / :func:`load_preset_for` / :func:`delete_preset_for`
  — the per-image parameter presets, stored as JSON in the project db's
  ``presets`` table via :mod:`seestar_controller.store.presets`;
- :func:`apply_preset_on_open` — the helper the editor controller calls when
  a stack is opened: the stored preset as a :class:`~.pipeline.Pipeline`, or
  None when the image has none (a corrupt preset is logged and ignored — the
  default pipeline is used then).

Store errors (:class:`~seestar_controller.store.errors.ProjectError`) are
wrapped into :class:`~.fits_io.EnhanceError` (with the cause chained), the
same convention as :func:`.fits_io.list_project_stacks`.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from seestar_controller.store.errors import ProjectError
from seestar_controller.store.frames import KIND_ENHANCED, Frame, store_frame
from seestar_controller.store.presets import (
    Preset,
    delete_preset,
    list_presets,
    load_preset,
    save_preset,
)
from seestar_controller.store.project import Project

from .fits_io import EnhanceError, FitsImage, StackInfo
from .pipeline import Pipeline
from .render import to_pil_image, tonemap

logger = logging.getLogger(__name__)

#: The export formats :func:`export_render` supports (uppercase PIL names).
EXPORT_FORMATS: tuple[str, ...] = ("PNG", "JPEG")

#: The default JPEG quality (1-100) of :func:`export_render`.
DEFAULT_EXPORT_QUALITY = 95

#: The ``HISTORY`` card prefix written into every saved enhanced FITS file.
ENHANCED_HISTORY = "enhanced by seestar-controller pipeline"

#: File suffixes accepted per export format (lowercase, with dot).
_EXPORT_SUFFIXES: dict[str, tuple[str, ...]] = {
    "PNG": (".png",),
    "JPEG": (".jpg", ".jpeg"),
}

#: FITS header cards that are NEVER copied from the source image into a saved
#: enhanced file — they describe the source HDU's storage, not the image, and
#: astropy recomputes them for the new data on write.
_STRUCTURAL_CARDS = frozenset(
    {
        "SIMPLE",
        "BITPIX",
        "NAXIS",
        "NAXIS1",
        "NAXIS2",
        "NAXIS3",
        "PCOUNT",
        "GCOUNT",
        "XTENSION",
        "EXTEND",
        "BSCALE",
        "BZERO",
        "BLANK",
        "CHECKSUM",
        "DATASUM",
    }
)


def _check_project(project: Project | None) -> Project:
    if not isinstance(project, Project):
        raise EnhanceError(
            f"save/export/preset needs an open store Project, got {project!r}"
        )
    return project


def _check_stack(stack: StackInfo | None) -> StackInfo:
    if not isinstance(stack, StackInfo):
        raise EnhanceError(f"needs an open StackInfo, got {stack!r}")
    return stack


def _check_pipeline(pipeline: Pipeline | None) -> Pipeline:
    if not isinstance(pipeline, Pipeline):
        raise EnhanceError(f"needs a Pipeline, got {pipeline!r}")
    return pipeline


def _enhanced_fits_bytes(pipeline: Pipeline, fits_image: FitsImage) -> bytes:
    """Render ``pipeline`` on ``fits_image`` at FULL resolution and serialize
    the float32 result as FITS bytes (lazy astropy import).

    The header is copied from the source image minus the structural cards
    (:data:`_STRUCTURAL_CARDS` — astropy recomputes them for the new data),
    and one ``HISTORY`` card records the enhancement
    (:data:`ENHANCED_HISTORY` + the op names).
    """
    from astropy.io import fits as pyfits

    out = pipeline.apply(fits_image.data)  # full resolution, float32, NaN-safe
    header = pyfits.Header()
    for card, value in fits_image.header.items():
        key = str(card)
        if key in _STRUCTURAL_CARDS or key.startswith("NAXIS"):
            continue
        try:
            header[key] = value
        except Exception:  # noqa: BLE001, S112
            continue  # an unassignable card (e.g. COMMENT/HISTORY handled below)
    header.add_history(f"{ENHANCED_HISTORY}: {', '.join(pipeline.names) or 'none'}")
    hdu = pyfits.PrimaryHDU(data=np.ascontiguousarray(out, dtype=np.float32), header=header)
    buf = io.BytesIO()
    hdu.writeto(buf)
    return buf.getvalue()


def save_enhanced(
    project: Project | None,
    stack: StackInfo,
    pipeline: Pipeline,
    fits_image: FitsImage,
) -> Frame:
    """Render ``pipeline`` on ``fits_image`` at FULL resolution and store the
    result as a new ``enhanced`` frame of ``stack``'s session in ``project``;
    returns the stored :class:`~seestar_controller.store.frames.Frame`.

    The original stack FITS file is NEVER touched — the enhanced image is a
    separate file under ``enhanced/<session_id>/`` (kind ``"enhanced"``, see
    :mod:`seestar_controller.store.frames`). The FITS header is copied from
    ``fits_image`` (structural cards skipped) with a ``HISTORY`` card
    recording the pipeline; the frame metadata (exposure/gain/target) is
    carried over from ``stack``.

    Raises :class:`EnhanceError` for bad argument types, an unreadable
    render, or a store failure (wrapped :class:`ProjectError`).
    """
    project = _check_project(project)
    stack = _check_stack(stack)
    pipeline = _check_pipeline(pipeline)
    if not isinstance(fits_image, FitsImage):
        raise EnhanceError(f"save_enhanced needs a FitsImage, got {fits_image!r}")
    try:
        fits_bytes = _enhanced_fits_bytes(pipeline, fits_image)
    except EnhanceError:
        raise
    except Exception as exc:
        raise EnhanceError(f"cannot render the enhanced image: {exc}") from exc
    meta: dict[str, Any] = {
        "exposure": stack.exposure,
        "gain": stack.gain,
        "target": stack.target,
    }
    try:
        return store_frame(project, stack.session_id, KIND_ENHANCED, fits_bytes, meta=meta)
    except ProjectError as exc:
        raise EnhanceError(
            f"cannot store the enhanced image of stack {stack.frame_id}: {exc}"
        ) from exc


def _as_pil_image(image: Image.Image | np.ndarray) -> Image.Image:
    """Normalize ``image`` to a PIL image: a PIL image passes through; a uint8
    array goes through :func:`~.render.to_pil_image`; a float array is
    tone-mapped first (:func:`~.render.tonemap`)."""
    if isinstance(image, Image.Image):
        return image
    arr = np.asarray(image)
    if arr.dtype == np.uint8:
        return to_pil_image(arr)
    if np.issubdtype(arr.dtype, np.floating):
        return to_pil_image(tonemap(arr))
    raise EnhanceError(
        f"export_render expects a PIL image, a uint8 array, or a float array, "
        f"got dtype {arr.dtype}"
    )


def export_render(
    image: Image.Image | np.ndarray,
    path: str | Path,
    format: str = "PNG",
    quality: int = DEFAULT_EXPORT_QUALITY,
) -> Path:
    """Export ``image`` (a PIL image, a uint8 array, or a float array —
    tone-mapped first) to the file ``path`` as PNG or JPEG; returns the
    :class:`~pathlib.Path` written.

    - ``format`` — one of :data:`EXPORT_FORMATS` (case-insensitive); anything
      else raises :class:`EnhanceError`;
    - ``quality`` — the JPEG quality (int 1-100, default
      :data:`DEFAULT_EXPORT_QUALITY`); ignored for PNG;
    - ``path`` — the target file. A missing suffix gets the format's default
      (``.png`` / ``.jpg``); a suffix that does not match the format (e.g.
      ``.png`` with ``format="JPEG"``) raises :class:`EnhanceError`.

    JPEG has no alpha channel: non-``L``/``RGB`` modes are converted to RGB.
    The write is atomic (temp file in the same directory + :func:`os.replace`,
    the same pattern as :func:`seestar_controller.store.frames.store_frame`).
    """
    fmt = str(format).upper()
    if fmt not in EXPORT_FORMATS:
        raise EnhanceError(
            f"unknown export format {format!r} — valid formats are "
            f"{', '.join(EXPORT_FORMATS)}"
        )
    if isinstance(quality, bool) or not isinstance(quality, int):
        raise EnhanceError(f"quality must be an int in 1..100, got {quality!r}")
    if not 1 <= quality <= 100:
        raise EnhanceError(f"quality must be in 1..100, got {quality!r}")
    pil_image = _as_pil_image(image)

    target = Path(path)
    suffix = target.suffix.lower()
    accepted = _EXPORT_SUFFIXES[fmt]
    if not suffix:
        target = target.with_suffix(accepted[0])
    elif suffix not in accepted:
        raise EnhanceError(
            f"file {target.name!r} has suffix {suffix!r} but format {fmt!r} "
            f"needs one of {', '.join(accepted)}"
        )

    if fmt == "JPEG" and pil_image.mode not in ("L", "RGB"):
        pil_image = pil_image.convert("RGB")
    save_kwargs: dict[str, Any] = {"format": fmt}
    if fmt == "JPEG":
        save_kwargs["quality"] = quality

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".export-", suffix=target.suffix)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        pil_image.save(tmp, **save_kwargs)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def save_preset_for(
    project: Project | None, stack: StackInfo, pipeline: Pipeline
) -> Preset:
    """Save (or replace) the enhancement preset of ``stack`` in ``project``:
    the pipeline serialized via :meth:`~.pipeline.Pipeline.to_dict` into the
    project db's ``presets`` table (see
    :func:`seestar_controller.store.presets.save_preset`). Returns the stored
    :class:`~seestar_controller.store.presets.Preset`.

    Raises :class:`EnhanceError` for bad argument types, an unknown frame, or
    a non-serializable pipeline (wrapped :class:`ProjectError`).
    """
    project = _check_project(project)
    stack = _check_stack(stack)
    pipeline = _check_pipeline(pipeline)
    try:
        return save_preset(project, stack.frame_id, pipeline.to_dict())
    except ProjectError as exc:
        raise EnhanceError(
            f"cannot save the preset of stack {stack.frame_id}: {exc}"
        ) from exc


def load_preset_for(project: Project, stack: StackInfo) -> Pipeline | None:
    """The stored enhancement preset of ``stack`` in ``project`` as a
    :class:`~.pipeline.Pipeline`, or None when the stack has no preset.

    Raises :class:`EnhanceError` for bad argument types or a corrupt stored
    preset (wrapped :class:`ProjectError`, or :class:`EnhanceError` from
    :meth:`~.pipeline.Pipeline.from_dict`).
    """
    project = _check_project(project)
    stack = _check_stack(stack)
    try:
        preset = load_preset(project, stack.frame_id)
    except ProjectError as exc:
        raise EnhanceError(
            f"cannot load the preset of stack {stack.frame_id}: {exc}"
        ) from exc
    if preset is None:
        return None
    return Pipeline.from_dict(preset.pipeline)


def delete_preset_for(project: Project, stack: StackInfo) -> bool:
    """Delete the stored preset of ``stack`` in ``project``; returns whether a
    preset was deleted. Raises :class:`EnhanceError` for bad argument types or
    a store failure (wrapped :class:`ProjectError`)."""
    project = _check_project(project)
    stack = _check_stack(stack)
    try:
        return delete_preset(project, stack.frame_id)
    except ProjectError as exc:
        raise EnhanceError(
            f"cannot delete the preset of stack {stack.frame_id}: {exc}"
        ) from exc


def apply_preset_on_open(
    project: Project | None, stack: StackInfo | None
) -> Pipeline | None:
    """The preset to apply when ``stack`` is opened in ``project``: the stored
    preset as a :class:`~.pipeline.Pipeline`, or None when there is none (no
    project, no stack, or no preset — then the caller uses the default
    pipeline).

    A CORRUPT preset never breaks opening the stack: it is logged and None is
    returned (the default pipeline is used).
    """
    if project is None or stack is None:
        return None
    try:
        return load_preset_for(project, stack)
    except EnhanceError as exc:
        logger.warning(
            "ignoring corrupt preset of stack %s: %s", stack.frame_id, exc
        )
        return None


__all__ = [
    "DEFAULT_EXPORT_QUALITY",
    "ENHANCED_HISTORY",
    "EXPORT_FORMATS",
    "apply_preset_on_open",
    "delete_preset_for",
    "export_render",
    "list_presets",
    "load_preset_for",
    "save_enhanced",
    "save_preset_for",
]
