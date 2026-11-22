"""Stacking subs into a stack offline — median-combine one session's
sub-frames into a ``kind='stack'`` FITS written back into the project
(``.scratch/sub-frames/issues/03-stack-subs.md``).

Strict pure layer (the sandbox has no GTK, see docs/wiki/assumptions.md):
NOTHING in this package imports ``gi`` — a subprocess test enforces the
boundary. The package is stdlib + :mod:`numpy` +
:mod:`seestar_controller.store`; the heavy :mod:`astropy.io.fits` import is
lazy inside :func:`.session.stack_session_subs`, the same pattern as
:mod:`seestar_controller.mosaic`.

Scope subs of one session are aligned by the telescope to within the
mount's tracking error, so the combine is a plain pixel-wise median by
default — and on an alt-az mount the FIELD ROTATES during the session, so
when the subs carry a WCS (solved ``frame_wcs`` record or header ``CD``
matrix) showing a total rotation beyond ~1 star FWHM, the subs are
derotated to a common rotation before the median (stacking issue 01); the
plain median remains the documented fallback when no WCS is available. The
product is a regular ``kind='stack'`` frame stored via
:func:`~seestar_controller.store.frames.store_frame`, so ALL stack-based
views work on it unchanged (Enhance viewer list, mosaic, report,
crossmatch, ...).

Modules:

- :mod:`.errors` — :class:`~.errors.StackSubsError`;
- :mod:`.core` — the pure combine math and field-rotation helpers:
  :func:`~.core.median_combine`, :func:`~.core.header_rotation_deg`,
  :func:`~.core.total_field_rotation_deg`,
  :func:`~.core.max_rotation_displacement_px`,
  :func:`~.core.rotation_exceeds_threshold`, :func:`~.core.derotate_array`,
  :data:`~.core.DEROTATE_THRESHOLD_PX`;
- :mod:`.session` — the project integration:
  :func:`~.session.stack_session_subs` and the records
  (:class:`~.session.StackSubsResult`, :class:`~.session.SkippedSub`).

Typical flow::

    from seestar_controller.stacking import stack_session_subs

    result = stack_session_subs(project, session_id)   # -> StackSubsResult
    result.frame                                       # the new stack Frame
    result.field_rotation_deg                          # total rotation (None w/o WCS)
    result.rotation_warning                            # blur warning, if any
"""

from __future__ import annotations

from .core import (
    DEROTATE_THRESHOLD_PX,
    derotate_array,
    header_rotation_deg,
    max_rotation_displacement_px,
    median_combine,
    rotation_exceeds_threshold,
    total_field_rotation_deg,
)
from .errors import StackSubsError
from .session import SkippedSub, StackSubsResult, stack_session_subs

__all__ = [
    "DEROTATE_THRESHOLD_PX",
    "SkippedSub",
    "StackSubsError",
    "StackSubsResult",
    "derotate_array",
    "header_rotation_deg",
    "max_rotation_displacement_px",
    "median_combine",
    "rotation_exceeds_threshold",
    "stack_session_subs",
    "total_field_rotation_deg",
]
