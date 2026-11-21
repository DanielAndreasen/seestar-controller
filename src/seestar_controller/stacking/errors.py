"""Errors of the stacking package (:mod:`seestar_controller.stacking`).

Pure Python — no GTK, no link (module boundary per
``.scratch/sub-frames/spec.md``).
"""

from __future__ import annotations


class StackSubsError(ValueError):
    """Any stack-subs failure — a :class:`ValueError` with a clear message:
    the session has no subs, no readable sub, or a combine shape mismatch.
    Structural type errors (a non-Project handle, a non-str session id)
    raise :class:`TypeError` instead; an unknown session id raises
    :class:`~seestar_controller.store.errors.SessionNotFoundError`."""
