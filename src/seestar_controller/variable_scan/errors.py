"""Errors of the variable-scan core (:mod:`seestar_controller.variable_scan`).

Pure Python — no GTK, no link (module boundary per
``.scratch/variable-scan/spec.md``).
"""

from __future__ import annotations


class ScanError(ValueError):
    """Any variable-scan failure — a :class:`ValueError` with a clear
    message: no cross-matched stack to take the source list from, an empty
    source list, every source coinciding with the picked target/comps, an
    invalid threshold / min-frames / radius argument, or a scan parameter
    that cannot be persisted. Structural type errors (a non-Project handle,
    a non-int frame id, ...) raise :class:`TypeError` instead."""
