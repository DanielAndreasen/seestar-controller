"""The composable enhancement pipeline — the PURE pipeline layer of
:mod:`seestar_controller.enhance` (image-enhance ticket 02).

This is a PURE module: **no ``gi`` import** (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary) — stdlib +
:mod:`numpy` + :mod:`PIL` + the sibling pure modules only. The widget layer
(:mod:`.viewer`) imports ``gi`` at module top and is never imported by this
module or eagerly by the package ``__init__``.

What lives here:

- :class:`Op` — one pipeline step: a frozen ``(name, params)`` pair, where
  ``name`` is one of :data:`~.ops.OP_NAMES` and ``params`` the matching
  params dataclass from :mod:`.ops` (validated at construction);
- :class:`Pipeline` — an ordered tuple of ops. :meth:`Pipeline.apply` runs
  the ops **in order, starting from the ORIGINAL image data every time**
  (each op is a pure function on a copy), so a pipeline is re-runnable and
  the input is never mutated. :meth:`Pipeline.with_param` returns a NEW
  pipeline with one op's params changed (immutable). Params and pipelines
  serialize to/from plain dicts (:meth:`to_dict` / :meth:`from_dict`) for
  ticket 04's per-image persistence;
- :func:`default_pipeline` — the sensible default op sequence;
- :func:`render_pipeline` — apply a pipeline to a
  :class:`~.fits_io.FitsImage` and render the result for preview
  (downscale first, then :func:`~.render.tonemap`, like
  :func:`~.render.render_preview`).

The ops themselves (stretch / denoise / sharpen / white balance / background
subtraction) live in :mod:`.ops` and work on FLOAT data; tone mapping for
display stays in :mod:`.render`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image

from .fits_io import EnhanceError, FitsImage
from .ops import (
    OP_NAMES,
    OPS,
    DenoiseParams,
    SharpenParams,
    StretchParams,
)
from .render import DEFAULT_MAX_PREVIEW_SIZE, downscale, to_pil_image, tonemap


@dataclass(frozen=True)
class Op:
    """One pipeline step: ``name`` (one of :data:`~.ops.OP_NAMES`) plus its
    ``params`` dataclass. Frozen; validated at construction (unknown name or
    mismatched params type -> :class:`EnhanceError`)."""

    name: str
    params: Any

    def __post_init__(self) -> None:
        spec = OPS.get(self.name)
        if spec is None:
            raise EnhanceError(
                f"unknown op name {self.name!r} — valid ops are {', '.join(OP_NAMES)}"
            )
        if not isinstance(self.params, spec.params_type):
            raise EnhanceError(
                f"op {self.name!r} expects {spec.params_type.__name__}, "
                f"got {type(self.params).__name__}"
            )

    @property
    def spec(self) -> Any:
        """The :class:`~.ops.OpSpec` of this op."""
        return OPS[self.name]

    def to_dict(self) -> dict[str, Any]:
        """``{"op": name, "params": {...}}`` — JSON-able."""
        return {"op": self.name, "params": self.params.to_dict()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Op:
        """Rebuild an :class:`Op` from :meth:`to_dict` output; unknown op
        names or malformed params raise :class:`EnhanceError`."""
        if not isinstance(data, Mapping):
            raise EnhanceError(f"Op.from_dict expects a dict, got {data!r}")
        name = data.get("op")
        spec = OPS.get(name) if isinstance(name, str) else None
        if spec is None:
            raise EnhanceError(
                f"Op.from_dict got unknown op {name!r} — valid ops are "
                f"{', '.join(OP_NAMES)}"
            )
        if "params" not in data:
            raise EnhanceError(f"Op.from_dict: missing 'params' in {data!r}")
        params = spec.params_type.from_dict(data["params"])
        return cls(name=name, params=params)


@dataclass(frozen=True)
class Pipeline:
    """An ordered, immutable sequence of :class:`Op` steps.

    :meth:`apply` runs the ops in order on a float32 copy of the input image
    (a :class:`~.fits_io.FitsImage` or a plain ndarray), starting from the
    ORIGINAL data on every call — so applying twice gives the same result and
    the input is never mutated. :meth:`with_param` returns a new
    :class:`Pipeline` with one op's params replaced (the receiver is
    unchanged).
    """

    ops: tuple[Op, ...] = ()

    def __post_init__(self) -> None:
        ops = tuple(self.ops)
        for op in ops:
            if not isinstance(op, Op):
                raise EnhanceError(
                    f"Pipeline ops must be Op instances, got {op!r}"
                )
        object.__setattr__(self, "ops", ops)

    @property
    def names(self) -> tuple[str, ...]:
        """The op names, in order."""
        return tuple(op.name for op in self.ops)

    def apply(self, image: FitsImage | np.ndarray) -> np.ndarray:
        """Apply the ops in order to ``image`` (a :class:`FitsImage` or an
        ndarray) and return the resulting float32 array.

        Every call starts from the ORIGINAL data of ``image`` (each op works
        on a copy), so the pipeline is re-runnable and ``image`` is never
        mutated. An empty pipeline returns a float32 copy of the input.
        """
        data = image.data if isinstance(image, FitsImage) else image
        out = np.array(data, dtype=np.float32, copy=True)
        for op in self.ops:
            out = op.spec.apply(out, op.params)
        return out

    def with_param(self, name: str, **changes: Any) -> Pipeline:
        """Return a NEW :class:`Pipeline` where the first op named ``name``
        has its params replaced by ``dataclasses.replace(params, **changes)``
        (re-validated, so invalid changes raise :class:`EnhanceError`).
        The receiver is unchanged. Raises :class:`EnhanceError` when no op
        with that name is in the pipeline."""
        for i, op in enumerate(self.ops):
            if op.name == name:
                new_params = dataclasses.replace(op.params, **changes)
                new_ops = self.ops[:i] + (Op(name, new_params),) + self.ops[i + 1 :]
                return Pipeline(new_ops)
        raise EnhanceError(
            f"pipeline has no op named {name!r} (ops: {list(self.names) or 'none'})"
        )

    def to_dict(self) -> dict[str, Any]:
        """``{"ops": [op.to_dict(), ...]}`` — JSON-able."""
        return {"ops": [op.to_dict() for op in self.ops]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Pipeline:
        """Rebuild a :class:`Pipeline` from :meth:`to_dict` output; malformed
        entries raise :class:`EnhanceError`."""
        if not isinstance(data, Mapping):
            raise EnhanceError(f"Pipeline.from_dict expects a dict, got {data!r}")
        raw_ops = data.get("ops", [])
        if not isinstance(raw_ops, Sequence) or isinstance(raw_ops, (str, bytes)):
            raise EnhanceError(f"Pipeline.from_dict: 'ops' must be a list, got {raw_ops!r}")
        return cls(ops=tuple(Op.from_dict(entry) for entry in raw_ops))


def default_pipeline() -> Pipeline:
    """The default enhancement pipeline: an asinh stretch, a gentle denoise,
    and a mild unsharp-mask sharpen (in that order)."""
    return Pipeline(
        ops=(
            Op("stretch", StretchParams(kind="asinh", sigma=10.0)),
            Op("denoise", DenoiseParams(strength=0.25, radius=1)),
            Op("sharpen", SharpenParams(amount=0.5, radius=1)),
        )
    )


def render_pipeline(
    pipeline: Pipeline,
    fits_image: FitsImage,
    max_size: int = DEFAULT_MAX_PREVIEW_SIZE,
) -> Image.Image:
    """Apply ``pipeline`` to ``fits_image`` and render the result as a PIL
    preview image: the pipeline runs on the full float data, then the result
    is downscaled to ``max_size`` FIRST and tone-mapped
    (:func:`~.render.tonemap`), exactly like
    :func:`~.render.render_preview` — so slider interaction stays smooth.
    """
    if not isinstance(pipeline, Pipeline):
        raise EnhanceError(f"render_pipeline expects a Pipeline, got {pipeline!r}")
    if not isinstance(fits_image, FitsImage):
        raise EnhanceError(f"render_pipeline expects a FitsImage, got {fits_image!r}")
    out = pipeline.apply(fits_image)
    return to_pil_image(tonemap(downscale(out, max_size)))
