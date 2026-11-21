"""Drift-plot rendering — matplotlib Agg -> PNG bytes (PURE, no ``gi``).

The PURE plot layer of astrometry ticket 03
(:mod:`seestar_controller.astrometry.plot`): :func:`render_drift_png` renders
an astrometry-02 :class:`~.drift.DriftSeries` (RA/Dec offset vs. time) to PNG
bytes with matplotlib's **Agg** backend — the same analysis-plotting pattern
as :mod:`seestar_controller.visibility.render` (``Figure`` +
``FigureCanvasAgg`` + ``fig.savefig``), so it works headless.

matplotlib enters LAZILY inside :func:`render_drift_png` (the module is
imported by the pure package ``__init__``, which must stay
matplotlib-free at import time — a subprocess boundary test enforces it).

The plot: two stacked subplots sharing the time axis — the RA offset (arcsec,
positive eastward) on top, the Dec offset (arcsec, positive northward) below
— vs. hours since the session's first solved frame, with zero lines, the
per-frame points, and a title carrying the session id, the solved-frame
count, and the least-squares drift rates in arcsec/hour.
"""

from __future__ import annotations

import io
from typing import Any

from .drift import DriftError, DriftSeries

#: The default render size / dpi (the PNG comes out exactly
#: ``width_px`` x ``height_px`` pixels).
DEFAULT_WIDTH_PX = 960
DEFAULT_HEIGHT_PX = 420
DEFAULT_DPI = 100

#: Hard cap for any render dimension (guards against absurd sizes).
MAX_RENDER_PX = 8000

#: Plot colors (RA / Dec curves).
RA_COLOR = "#4c9aff"
DEC_COLOR = "#ffb84c"
ZERO_COLOR = "#888888"


def _check_px(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_RENDER_PX:
        raise DriftError(f"{name} must be an int in [1, {MAX_RENDER_PX}], got {value!r}")
    return value


def _format_rate(value: float | None) -> str:
    return f"{value:+.2f}\u2033/h" if value is not None else "n/a"


def render_drift_png(
    series: DriftSeries,
    *,
    width_px: int = DEFAULT_WIDTH_PX,
    height_px: int = DEFAULT_HEIGHT_PX,
    dpi: int = DEFAULT_DPI,
) -> bytes:
    """Render ``series`` to PNG bytes (matplotlib Agg, no display needed).

    - ``width_px`` / ``height_px`` / ``dpi`` — the render size (the figure is
      ``width_px / dpi`` x ``height_px / dpi`` inches at ``dpi``, so the PNG
      comes out exactly ``width_px`` x ``height_px`` pixels). Values outside
      ``[1, MAX_RENDER_PX]`` raise :class:`~.drift.DriftError`.

    Returns the PNG file contents as ``bytes``. Raises
    :class:`~.drift.DriftError` when ``series`` is not a
    :class:`~.drift.DriftSeries` or a size is invalid.
    """
    if not isinstance(series, DriftSeries):
        raise DriftError(f"series must be a DriftSeries, got {series!r}")
    width_px = _check_px(width_px, "width_px")
    height_px = _check_px(height_px, "height_px")
    dpi = _check_px(dpi, "dpi")

    import matplotlib

    matplotlib.use("Agg")
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    fig = Figure(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
    FigureCanvasAgg(fig)  # attach the Agg canvas (savefig works headless)
    ax_ra = fig.add_subplot(2, 1, 1)
    ax_dec = fig.add_subplot(2, 1, 2, sharex=ax_ra)

    points = series.points
    hours = [p.hours for p in points]
    ra = [p.ra_offset_arcsec for p in points]
    dec = [p.dec_offset_arcsec for p in points]
    if points:
        ax_ra.plot(hours, ra, "o-", color=RA_COLOR, markersize=4, linewidth=1.2, label="RA offset")
        ax_dec.plot(hours, dec, "s-", color=DEC_COLOR, markersize=4, linewidth=1.2, label="Dec offset")
    else:
        ax_ra.text(
            0.5, 0.5, "no solved frames", transform=ax_ra.transAxes,
            ha="center", va="center",
        )
    for ax in (ax_ra, ax_dec):
        ax.axhline(0.0, color=ZERO_COLOR, linewidth=0.8, linestyle="--", zorder=0)
        ax.grid(True, alpha=0.3)
    ax_ra.set_ylabel("RA offset (\u2033)")
    ax_dec.set_ylabel("Dec offset (\u2033)")
    ax_dec.set_xlabel("hours since first solved frame")
    ax_ra.legend(loc="upper right", fontsize="small")
    ax_dec.legend(loc="upper right", fontsize="small")
    ax_ra.set_title(
        f"Drift — session {series.session_id} · "
        f"{series.n_solved}/{series.n_frames} solved"
        + (f" · {series.n_missing_wcs} without WCS" if series.n_missing_wcs else "")
        + f"\nRA {_format_rate(series.ra_drift_arcsec_per_hour)} · "
        f"Dec {_format_rate(series.dec_drift_arcsec_per_hour)}",
        fontsize="medium",
    )
    fig.tight_layout()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png")
    return buffer.getvalue()


__all__ = [
    "DEFAULT_DPI",
    "DEFAULT_HEIGHT_PX",
    "DEFAULT_WIDTH_PX",
    "MAX_RENDER_PX",
    "render_drift_png",
]
