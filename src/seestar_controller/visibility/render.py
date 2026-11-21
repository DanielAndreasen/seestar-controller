"""The night-timeline renderer — matplotlib Agg -> PNG bytes (PURE, no ``gi``).

This is part of the PURE layer of :mod:`seestar_controller.visibility`: **no
``gi`` import** — stdlib + :mod:`numpy` + :mod:`astropy` + :mod:`matplotlib`
(Agg canvas only, no pyplot, no display) + the sibling pure modules only —
so it is fully unit-testable headless (the sandbox has no GTK, see
docs/wiki/assumptions.md; a subprocess test enforces the boundary). No
network either.

What lives here:

- :func:`render_timeline_png` — render a :class:`~.timeline.TimelineModel` to
  **PNG bytes** with matplotlib's Agg backend
  (:class:`matplotlib.figure.Figure` + :class:`~matplotlib.backends.backend_agg.FigureCanvasAgg`
  — no pyplot, no GUI backend, no global state): the x-axis is local time over
  the night window (hours since local noon, ticked every 2 h with local
  ``HH:MM`` labels), the y-axis is altitude in degrees with the horizon at
  0°, the **astronomical twilight** is shaded (evening dusk → morning dawn,
  annotated with the local times), one **altitude curve per queued target**
  (deterministic palette, legend with the entry names), and the **moon track**
  as a dashed grey curve annotated with its illuminated fraction at culmination.

The GTK widget (:mod:`.timeline_view`) renders the model through this function
and wraps the PNG in a PIL image -> :class:`Gdk.Texture` (the same
PNG -> PIL -> texture path as the image-enhance viewer); the PNG bytes are
also what the **Export PNG** button writes to disk.
"""

from __future__ import annotations

import io
import math
from typing import Any

import astropy.units as u
import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from seestar_controller.visibility.site import VisibilityError
from seestar_controller.visibility.timeline import TimelineModel, format_local

#: Default render width, in pixels.
DEFAULT_WIDTH_PX = 960
#: Default render height, in pixels.
DEFAULT_HEIGHT_PX = 420
#: Default render DPI (the figure size is ``width_px / dpi`` inches).
DEFAULT_DPI = 100
#: Maximum render dimension, in pixels (sanity cap).
MAX_RENDER_PX = 8000

#: The deterministic target-curve palette (cycled per queued target).
TIMELINE_PALETTE: tuple[str, ...] = (
    "#1f77b4",
    "#d62728",
    "#2ca02c",
    "#9467bd",
    "#ff7f0e",
    "#8c564b",
    "#17becf",
    "#e377c2",
)

#: The moon track color (dashed grey — it is an annotation, not a target).
MOON_COLOR = "#777777"
#: The astronomical-twilight shading color (dark blue, translucent).
TWILIGHT_COLOR = "#223a70"
#: The horizon line color.
HORIZON_COLOR = "#444444"

#: X-axis tick step, in hours.
X_TICK_STEP_HOURS = 2.0


def _check_px(value: Any, name: str) -> int:
    """Validate a render dimension (pixels); raises :class:`VisibilityError`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VisibilityError(f"{name} must be a number of pixels, got {value!r}")
    px = float(value)
    if not math.isfinite(px) or not 1 <= px <= MAX_RENDER_PX:
        raise VisibilityError(
            f"{name} must be in [1, {MAX_RENDER_PX}] pixels, got {value!r}"
        )
    return int(px)


def _twilight_spans(model: TimelineModel) -> tuple[tuple[float, float], ...]:
    """The astronomical-twilight spans of the night in axis hours
    ``(start_hour, end_hour)`` — empty when the sun never reaches −18°
    (polar day/night, ``astro_start`` / ``astro_end`` are ``None``)."""
    spans: list[tuple[float, float]] = []
    twilight = model.twilight
    if twilight.astro_start is not None and twilight.astro_end is not None:
        start_h = float((twilight.astro_start - model.window[0]).to_value(u.hour))
        end_h = float((twilight.astro_end - model.window[0]).to_value(u.hour))
        spans.append((start_h, end_h))
    return tuple(spans)


def _moon_phase_at_culmination(model: TimelineModel) -> float:
    """The moon's illuminated fraction at its culmination (max altitude) on
    the night grid, in ``[0, 1]``."""
    alt = model.moon.alt.to_value(u.deg)
    if alt.size == 0:
        return 0.0
    return float(np.asarray(model.moon.phase.to_value(u.dimensionless_unscaled))[int(np.argmax(alt))])


def render_timeline_png(
    model: TimelineModel,
    *,
    width_px: int = DEFAULT_WIDTH_PX,
    height_px: int = DEFAULT_HEIGHT_PX,
    dpi: int = DEFAULT_DPI,
) -> bytes:
    """Render ``model`` to PNG bytes (matplotlib Agg, no display needed).

    - ``width_px`` / ``height_px`` / ``dpi`` — the render size (the figure is
      ``width_px / dpi`` × ``height_px / dpi`` inches at ``dpi``, so the PNG
      comes out exactly ``width_px`` × ``height_px`` pixels). Values outside
      ``[1, MAX_RENDER_PX]`` raise :class:`VisibilityError`.

    The plot: local-time x-axis over the night window (0–24 h since local
    noon, ticks every :data:`X_TICK_STEP_HOURS` h labeled with local
    ``HH:MM``), altitude y-axis with the horizon at 0°, the astronomical
    twilight shaded between dusk and dawn (annotated with the local times),
    one altitude curve per resolved queued target (palette + legend), and the
    moon track (dashed) annotated with its illuminated fraction at
    culmination. Returns the PNG file contents as ``bytes``.
    """
    if not isinstance(model, TimelineModel):
        raise VisibilityError(f"model must be a TimelineModel, got {model!r}")
    width_px = _check_px(width_px, "width_px")
    height_px = _check_px(height_px, "height_px")
    dpi = _check_px(dpi, "dpi")

    fig = Figure(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
    FigureCanvasAgg(fig)  # attach the Agg canvas (print_png works headless)
    ax = fig.add_subplot(1, 1, 1)

    hours = np.asarray(model.axis_hours, dtype=float)

    # astronomical twilight shading (behind everything else)
    for start_h, end_h in _twilight_spans(model):
        ax.axvspan(start_h, end_h, color=TWILIGHT_COLOR, alpha=0.22, zorder=0)
    twilight = model.twilight
    if twilight.astro_start is not None:
        ax.annotate(
            f"astro dusk {format_local(model.site, twilight.astro_start)}",
            xy=(float((twilight.astro_start - model.window[0]).to_value(u.hour)), -88),
            color=TWILIGHT_COLOR,
            fontsize=7,
            ha="left",
        )
    if twilight.astro_end is not None:
        ax.annotate(
            f"astro dawn {format_local(model.site, twilight.astro_end)}",
            xy=(float((twilight.astro_end - model.window[0]).to_value(u.hour)), -88),
            color=TWILIGHT_COLOR,
            fontsize=7,
            ha="right",
        )

    # horizon + altitude range
    ax.axhline(0.0, color=HORIZON_COLOR, linewidth=0.8, zorder=1)
    ax.set_ylim(-90.0, 90.0)
    ax.set_xlim(0.0, 24.0)

    # one altitude curve per resolved queued target
    for i, curve in enumerate(model.curves):
        color = TIMELINE_PALETTE[i % len(TIMELINE_PALETTE)]
        ax.plot(hours, curve.alt.to_value(u.deg), color=color, linewidth=1.6, label=curve.name)

    # the moon track (dashed) + phase annotation at culmination
    phase = _moon_phase_at_culmination(model)
    ax.plot(
        hours,
        model.moon.alt.to_value(u.deg),
        color=MOON_COLOR,
        linewidth=1.2,
        linestyle="--",
        label=f"Moon {phase * 100.0:.0f}%",
    )

    # axes: local time on x, altitude on y
    ax.set_xlabel(f"local time ({model.site.timezone}) — night of {model.night.isoformat()}")
    ax.set_ylabel("altitude (deg)")
    tick_hours = np.arange(0.0, 24.0 + X_TICK_STEP_HOURS / 2.0, X_TICK_STEP_HOURS)
    ax.set_xticks(tick_hours)
    ax.set_xticklabels(
        [format_local(model.site, model.window[0] + float(h) * u.hour) for h in tick_hours],
        fontsize=7,
    )
    ax.tick_params(axis="y", labelsize=7)
    ax.set_title(f"Night timeline {model.night.isoformat()} — {model.site}", fontsize=9)
    if model.curves:
        ax.legend(loc="upper right", fontsize=7, framealpha=0.9)

    fig.tight_layout()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png")
    return buffer.getvalue()
