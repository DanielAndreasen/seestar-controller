"""Report plots — matplotlib (Agg backend) PNG rendering for session-report
ticket 01 (:mod:`seestar_controller.report.plots`).

This is a PURE module: **no ``gi`` import** (a subprocess test enforces the
boundary). :mod:`matplotlib` is imported LAZILY inside the plotting
functions with the **Agg** backend forced first (headless — no display, no
GUI toolkit), so importing this package never pulls a GUI backend and never
imports ``gi``. Points are duck-typed: anything with ``jd`` / ``mag`` /
``mag_err`` / ``excluded`` / ``session_id`` works (the store's
:class:`~seestar_controller.store.measurements.Measurement` and the analysis
:class:`~seestar_controller.analysis.lightcurve.LightcurvePoint` both do;
folded points are unwrapped from
:class:`~seestar_controller.analysis.fold.FoldedPoint`).

What lives here:

- :func:`lightcurve_png` — the lightcurve plot (mag vs JD, inverted mag axis
  — brighter at the top, matching the app's plot convention — one color per
  session from :data:`~seestar_controller.analysis.lightcurve.SESSION_PALETTE`,
  error bars when ``mag_err`` is known, excluded outliers in the excluded
  color with an ``x`` marker) as PNG bytes;
- :func:`fold_png` — the phase-fold overlay plot (mag vs phase in ``[0, 1)``,
  same conventions, titled with the period) as PNG bytes;
- :func:`quality_trend_png` (ticket 02) — the frame-quality SPARKLINE: FWHM
  (px) vs JD over the session's scored frames, flagged (rejected) frames in
  the excluded color with an ``x`` marker, as PNG bytes.

All render a valid PNG even for an EMPTY point set (an axes with a "no
data" note), so the HTML always has an image for the section.
"""

from __future__ import annotations

import io
from collections.abc import Iterable
from typing import Any

from seestar_controller.analysis.lightcurve import (
    EXCLUDED_COLOR,
    session_color,
)

#: Figure size (inches) of the lightcurve plot.
LIGHTCURVE_FIGSIZE = (7.2, 3.6)
#: Figure size (inches) of the fold plot.
FOLD_FIGSIZE = (4.8, 3.6)
#: Figure size (inches) of the frame-quality sparkline (wide and short).
QUALITY_FIGSIZE = (7.2, 1.8)
#: DPI of the rendered PNGs.
PNG_DPI = 110


def _pyplot():
    """Import matplotlib with the Agg backend forced (headless) and return
    its ``pyplot`` module. Lazy on purpose: importing this package must stay
    GUI-free and cheap."""
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    return plt


def _fig_to_png(fig: Any) -> bytes:
    """Render ``fig`` to PNG bytes and close it (never leaks figures)."""
    buf = io.BytesIO()
    try:
        fig.savefig(buf, format="png", dpi=PNG_DPI, bbox_inches="tight")
    finally:
        _pyplot().close(fig)
    return buf.getvalue()


def _sessions_first_seen(points: Iterable[Any]) -> tuple[str, ...]:
    """The session ids of ``points`` in first-seen order."""
    seen: list[str] = []
    for point in points:
        sid = getattr(point, "session_id", "")
        if sid not in seen:
            seen.append(sid)
    return tuple(seen)


def _scatter_points(ax: Any, xs: list[float], ys: list[float], yerrs: list[float | None],
                    colors: list[Any], markers: list[str]) -> None:
    """Scatter with per-point colors/markers + optional error bars (None
    entries are skipped for the error bars)."""
    for x, y, yerr, color, marker in zip(xs, ys, yerrs, colors, markers):
        if yerr is not None:
            ax.errorbar(
                [x], [y], yerr=[yerr], fmt=marker, color=color, ecolor=color,
                elinewidth=0.8, capsize=2, markersize=5, linestyle="none",
            )
        else:
            ax.plot([x], [y], marker=marker, color=color, markersize=5, linestyle="none")


def lightcurve_png(points: Iterable[Any], *, title: str | None = None) -> bytes:
    """Render the lightcurve plot of ``points`` (mag vs JD) to PNG bytes.

    - inverted mag axis (brighter at the top);
    - one color per session (first-seen order, the analysis palette);
    - error bars for points with a known ``mag_err``;
    - excluded (outlier) points in :data:`~seestar_controller.analysis.lightcurve.EXCLUDED_COLOR`
      with an ``x`` marker;
    - an empty point set still renders a valid PNG (axes + "no measurements"
      note).

    ``title`` overrides the default axes title.
    """
    plt = _pyplot()
    pts = tuple(points)
    sessions = _sessions_first_seen(pts)
    fig, ax = plt.subplots(figsize=LIGHTCURVE_FIGSIZE)
    if not pts:
        ax.text(0.5, 0.5, "no measurements", transform=ax.transAxes,
                ha="center", va="center", color="0.45")
    else:
        xs: list[float] = []
        ys: list[float] = []
        yerrs: list[float | None] = []
        colors: list[Any] = []
        markers: list[str] = []
        for p in pts:
            xs.append(float(p.jd))
            ys.append(float(p.mag))
            yerr = getattr(p, "mag_err", None)
            yerrs.append(None if yerr is None else float(yerr))
            if getattr(p, "excluded", False):
                colors.append(EXCLUDED_COLOR)
                markers.append("x")
            else:
                colors.append(session_color(sessions, str(getattr(p, "session_id", ""))))
                markers.append("o")
        _scatter_points(ax, xs, ys, yerrs, colors, markers)
        ax.set_xlim(min(xs) - 0.02 * (max(xs) - min(xs) or 1.0),
                    max(xs) + 0.02 * (max(xs) - min(xs) or 1.0))
    ax.invert_yaxis()  # brighter (smaller mag) at the top
    ax.set_xlabel("JD")
    ax.set_ylabel("differential mag")
    ax.set_title(title or "Lightcurve")
    ax.grid(True, alpha=0.3)
    return _fig_to_png(fig)


def fold_png(folded: Iterable[Any], period: float, *, title: str | None = None) -> bytes:
    """Render the phase-fold overlay of ``folded`` points (mag vs phase) to
    PNG bytes, titled with ``period`` (days).

    ``folded`` items are :class:`~seestar_controller.analysis.fold.FoldedPoint`\\ s
    (``.point`` / ``.phase``) or plain points with a ``phase`` attribute.
    Same plot conventions as :func:`lightcurve_png`; an empty fold still
    renders a valid PNG.
    """
    plt = _pyplot()
    items = tuple(folded)
    sessions = _sessions_first_seen([getattr(item, "point", item) for item in items])
    fig, ax = plt.subplots(figsize=FOLD_FIGSIZE)
    if not items:
        ax.text(0.5, 0.5, "no points", transform=ax.transAxes,
                ha="center", va="center", color="0.45")
    else:
        xs: list[float] = []
        ys: list[float] = []
        yerrs: list[float | None] = []
        colors: list[Any] = []
        markers: list[str] = []
        for item in items:
            point = getattr(item, "point", item)
            phase = float(getattr(item, "phase", 0.0))
            xs.append(phase)
            ys.append(float(point.mag))
            yerr = getattr(point, "mag_err", None)
            yerrs.append(None if yerr is None else float(yerr))
            if getattr(point, "excluded", False):
                colors.append(EXCLUDED_COLOR)
                markers.append("x")
            else:
                colors.append(
                    session_color(sessions, str(getattr(point, "session_id", "")))
                )
                markers.append("o")
        _scatter_points(ax, xs, ys, yerrs, colors, markers)
        ax.set_xlim(-0.02, 1.02)
    ax.invert_yaxis()
    ax.set_xlabel("phase")
    ax.set_ylabel("differential mag")
    ax.set_title(title or f"Phase fold — P = {period:.6g} d")
    ax.grid(True, alpha=0.3)
    return _fig_to_png(fig)


def quality_trend_png(points: Iterable[Any], *, title: str | None = None) -> bytes:
    """Render the frame-quality sparkline of ``points`` (FWHM in px vs JD)
    to PNG bytes (ticket 02).

    ``points`` are duck-typed quality points (anything with ``jd`` / ``fwhm``
    / ``flagged`` — e.g. :class:`~seestar_controller.report.data.QualityPoint`).
    Points without a FWHM (``fwhm`` None) are skipped; flagged (rejected)
    frames are drawn in :data:`~seestar_controller.analysis.lightcurve.EXCLUDED_COLOR`
    with an ``x`` marker, the rest as plain dots. An empty point set (or no
    FWHM at all) still renders a valid PNG (axes + a "no FWHM" note).
    """
    plt = _pyplot()
    pts = tuple(points)
    fig, ax = plt.subplots(figsize=QUALITY_FIGSIZE)
    xs: list[float] = []
    ys: list[float] = []
    colors: list[Any] = []
    markers: list[str] = []
    for p in pts:
        fwhm = getattr(p, "fwhm", None)
        if fwhm is None:
            continue
        xs.append(float(p.jd))
        ys.append(float(fwhm))
        if getattr(p, "flagged", False):
            colors.append(EXCLUDED_COLOR)
            markers.append("x")
        else:
            colors.append("tab:blue")
            markers.append("o")
    if not xs:
        ax.text(0.5, 0.5, "no FWHM measurements", transform=ax.transAxes,
                ha="center", va="center", color="0.45")
    else:
        _scatter_points(ax, xs, ys, [None] * len(xs), colors, markers)
        span = max(xs) - min(xs)
        ax.set_xlim(min(xs) - 0.02 * (span or 1.0), max(xs) + 0.02 * (span or 1.0))
    ax.set_xlabel("JD")
    ax.set_ylabel("FWHM (px)")
    ax.set_title(title or "Frame quality trend")
    ax.grid(True, alpha=0.3)
    return _fig_to_png(fig)
