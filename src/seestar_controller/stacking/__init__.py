"""Stacking subs into a stack offline — median-combine one session's
sub-frames into a ``kind='stack'`` FITS written back into the project
(``.scratch/sub-frames/issues/03-stack-subs.md``).

Strict pure layer (the sandbox has no GTK, see docs/wiki/assumptions.md):
NOTHING in this package imports ``gi`` — a subprocess test enforces the
boundary. The package is stdlib + :mod:`numpy` +
:mod:`seestar_controller.store`; the heavy :mod:`astropy.io.fits` import is
lazy inside :func:`.session.stack_session_subs`, the same pattern as
:mod:`seestar_controller.mosaic`.

Scope subs of one session are already aligned by the telescope, so the
combine is a plain pixel-wise median — NO registration (documented
assumption). The product is a regular ``kind='stack'`` frame stored via
:func:`~seestar_controller.store.frames.store_frame`, so ALL stack-based
views work on it unchanged (Enhance viewer list, mosaic, report,
crossmatch, ...).

Modules:

- :mod:`.errors` — :class:`~.errors.StackSubsError`;
- :mod:`.core` — the pure combine math: :func:`~.core.median_combine`;
- :mod:`.session` — the project integration:
  :func:`~.session.stack_session_subs` and the records
  (:class:`~.session.StackSubsResult`, :class:`~.session.SkippedSub`).

Typical flow::

    from seestar_controller.stacking import stack_session_subs

    result = stack_session_subs(project, session_id)   # -> StackSubsResult
    result.frame                                       # the new stack Frame
"""

from __future__ import annotations

from .core import median_combine
from .errors import StackSubsError
from .session import SkippedSub, StackSubsResult, stack_session_subs

__all__ = [
    "SkippedSub",
    "StackSubsError",
    "StackSubsResult",
    "median_combine",
    "stack_session_subs",
]
