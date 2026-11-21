"""The pure median-combine math of the stacking package.

Pure Python — no GTK, no link, no astropy: stdlib + :mod:`numpy` only (the
same boundary as :mod:`seestar_controller.mosaic.core`). The combine is a
plain pixel-wise **median** over the leading axis: scope subs of one session
are already aligned by the telescope (no registration — documented
assumption, see ``.scratch/sub-frames/issues/03-stack-subs.md``), so the
median is taken at the SAME pixel of every sub, which also rejects
cosmic-ray hits and satellite trails for free.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import numpy as np

from .errors import StackSubsError


def median_combine(arrays: Iterable[Any]) -> np.ndarray:
    """Median-combine ``arrays`` (image arrays of EQUAL shape) pixel-wise:
    ``median(arrays, axis=0)`` over a new leading axis.

    The result keeps the input dtype for floating inputs (the median of an
    even number of frames averages the two middle values, then casts back).

    Raises :class:`TypeError` when ``arrays`` is not an iterable of numpy
    arrays, and :class:`~.errors.StackSubsError` when the sequence is empty
    or the shapes differ.
    """
    if isinstance(arrays, (str, bytes)) or not isinstance(arrays, Iterable):
        raise TypeError(f"arrays must be an iterable of numpy arrays, got {arrays!r}")
    arrs = list(arrays)
    if not arrs:
        raise StackSubsError("median_combine of an empty sequence")
    for arr in arrs:
        if not isinstance(arr, np.ndarray):
            raise TypeError(
                f"arrays must contain only numpy arrays, got {type(arr).__name__}"
            )
    shape = arrs[0].shape
    for arr in arrs[1:]:
        if arr.shape != shape:
            raise StackSubsError(
                f"shape mismatch: {arr.shape} does not match the first array's {shape}"
            )
    combined = np.median(np.stack(arrs), axis=0)
    if np.issubdtype(arrs[0].dtype, np.floating) and combined.dtype != arrs[0].dtype:
        combined = combined.astype(arrs[0].dtype)
    return combined
