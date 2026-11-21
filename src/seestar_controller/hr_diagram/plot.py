"""The H-R diagram plot renderer — matplotlib Agg -> PNG bytes (PURE, no ``gi``).

This is part of the PURE layer of :mod:`seestar_controller.hr_diagram` (ticket
03): **no ``gi`` import** — stdlib + :mod:`matplotlib` (Agg
canvas only, no pyplot, no display) + :mod:`PIL` + the sibling pure modules
(:mod:`.core`, :mod:`.isochrone`) only — so it is fully unit-testable headless
(the sandbox has no GTK, see docs/wiki/assumptions.md; a subprocess test
enforces the boundary). No network either.

What lives here:

- the diagram kinds :data:`CMD` / :data:`HR` and :func:`point_xy` (the data
  coordinates of one :class:`~.core.HrPoint` on each page);
- the plot transform math — :class:`DiagramGeometry`, :class:`DiagramWindow`,
  :func:`full_window`, :func:`data_to_screen`, :func:`screen_to_data`,
  :func:`zoom_window_at`, :func:`pan_window`, :func:`point_at` — the SAME
  pure-transform pattern as :mod:`seestar_controller.analysis.lightcurve`: the
  widget only feeds it events, the math is unit-tested headless. The magnitude
  axis is INVERTED (brighter at the top, the results-view convention);
- :func:`render_diagram_png` — render a :class:`DiagramModel` to PNG bytes with
  matplotlib's Agg backend. The figure's ``subplots_adjust`` is set so the axes
  rectangle is EXACTLY the :class:`DiagramGeometry` inner plot rectangle, so
  :func:`data_to_screen` and matplotlib's own transform agree pixel-for-pixel
  (that is what makes the click hit-testing exact);
- :func:`render_stack_png` — the stack image with the cross-match markers drawn
  (PIL): the "other half" of the click-to-highlight coordination — the markers
  sit at the crossmatch table's pixel positions (``HrPoint.x`` / ``.y``,
  scaled with the image), and the highlighted star gets a ring;
- :func:`nearest_point_by_pixel` — the image-side hit-test (click on the stack
  image -> the nearest crossmatched star within a radius, in image pixels).

Documented choices:

- **Error bars.** The cross-match table's only per-star error is the parallax
  error (there are no photometric errors in the ``crossmatch`` schema), so the
  error-bar toggle draws the parallax-propagated magnitude uncertainty
  :math:`\\sigma_M = 5\\,\\sigma_{plx} / (\\ln 10 \\cdot plx)` on the magnitude
  axis of BOTH pages — on the H-R page this is exactly the abs-G error; on the
  CMD page it is the distance-modulus uncertainty shown on the apparent-G
  axis (see :func:`abs_g_error`).
- **Overlay.** The isochrone overlay is drawn on both pages: apparent ``g``
  vs BP-RP on the CMD page, absolute ``abs_g`` vs BP-RP on the H-R page (the
  :class:`~.isochrone.IsochronePoint` carries both).
"""

from __future__ import annotations

import io
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from PIL import Image, ImageDraw

from .core import HrError, HrPoint
from .isochrone import IsochronePoint

#: The apparent CMD page (G vs BP-RP).
CMD = "cmd"
#: The H-R page (absolute G vs BP-RP).
HR = "hr"
#: The diagram kinds, in page order.
KINDS = (CMD, HR)

#: Default render width, in pixels.
DEFAULT_WIDTH_PX = 720
#: Default render height, in pixels.
DEFAULT_HEIGHT_PX = 560
#: Default render DPI (the figure size is ``width_px / dpi`` inches).
DEFAULT_DPI = 100
#: Maximum render dimension, in pixels (sanity cap).
MAX_RENDER_PX = 8000

#: Plot margins (pixels): left (mag axis labels), right, top (title), bottom
#: (color axis label + ticks).
PLOT_MARGIN_LEFT = 64
PLOT_MARGIN_RIGHT = 16
PLOT_MARGIN_TOP = 30
PLOT_MARGIN_BOTTOM = 46

#: The zoom factor per scroll step (``ZOOM_FACTOR ** steps``; scrolling up
#: zooms in).
ZOOM_FACTOR = 1.25

#: Minimum window spans (zoom-in clamp), in mag / color units.
MIN_X_SPAN = 1e-6
MIN_Y_SPAN = 1e-6

#: Hit-test radius (pixels) around a data point for the click-to-highlight
#: gesture on the diagram.
POINT_HIT_RADIUS_PX = 8.0

#: Hit-test radius (image pixels) around a star marker for the click gesture on
#: the stack image.
STACK_HIT_RADIUS_PX = 10.0

#: Maximum long side (pixels) of the rendered stack image (markers are drawn
#: AFTER the downscale, so they stay crisp).
DEFAULT_STACK_MAX_PX = 480

#: Cluster-member point color (blue).
MEMBER_COLOR = "#1f77b4"
#: Non-member (field star) point color (grey).
FIELD_COLOR = "#999999"
#: The isochrone overlay color (red, dashed).
OVERLAY_COLOR = "#d62728"
#: The highlighted star color (orange).
HIGHLIGHT_COLOR = "#ff7f0e"
#: The stack-image marker color (green) and highlight ring color (orange).
STACK_MARKER_COLOR = (80, 220, 100)
STACK_HIGHLIGHT_COLOR = (255, 140, 0)


def _check_kind(kind: Any) -> str:
    """Validate a diagram kind; raises :class:`TypeError` / :class:`HrError`."""
    if not isinstance(kind, str):
        raise TypeError(f"kind must be a string, got {kind!r}")
    if kind not in KINDS:
        raise HrError(f"kind must be one of {KINDS}, got {kind!r}")
    return kind


def point_xy(kind: Any, point: HrPoint) -> tuple[float, float]:
    """The data coordinates ``(x, y)`` of ``point`` on the ``kind`` page:
    ``(bp_rp, g)`` for :data:`CMD`, ``(bp_rp, abs_g)`` for :data:`HR`.

    Raises :class:`HrError` on the H-R page when ``abs_g`` is None (a CMD
    point without a positive parallax has no H-R position).
    """
    kind = _check_kind(kind)
    abs_g = point.abs_g
    if kind == HR:
        if abs_g is None:
            raise HrError(
                f"point {point.source_id} has no abs_g — it has no H-R position "
                "(no positive parallax)"
            )
        return point.bp_rp, float(abs_g)
    return point.bp_rp, point.g


def abs_g_error(point: HrPoint) -> float | None:
    """The parallax-propagated magnitude uncertainty
    ``sigma_M = 5 * parallax_error / (ln(10) * parallax)`` of ``point``, or
    None when the row has no positive parallax + error (no error bar)."""
    parallax = point.parallax
    parallax_error = point.parallax_error
    if parallax is None or parallax_error is None or parallax <= 0:
        return None
    return 5.0 * parallax_error / (math.log(10.0) * parallax)


# --------------------------------------------------------------------------------------
# Plot transform math (pure — the widget only feeds it events)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DiagramGeometry:
    """The plot's pixel geometry: the drawing area size plus the margins.

    ``plot_width`` / ``plot_height`` are the inner plot rectangle (at least
    1 px, so the transforms never divide by zero).
    """

    width: int
    height: int
    margin_left: int = PLOT_MARGIN_LEFT
    margin_right: int = PLOT_MARGIN_RIGHT
    margin_top: int = PLOT_MARGIN_TOP
    margin_bottom: int = PLOT_MARGIN_BOTTOM

    def __post_init__(self) -> None:
        for name in (
            "width",
            "height",
            "margin_left",
            "margin_right",
            "margin_top",
            "margin_bottom",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise HrError(f"{name} must be a non-negative int, got {value!r}")

    @property
    def plot_width(self) -> int:
        """The inner plot width in pixels (at least 1)."""
        return max(self.width - self.margin_left - self.margin_right, 1)

    @property
    def plot_height(self) -> int:
        """The inner plot height in pixels (at least 1)."""
        return max(self.height - self.margin_top - self.margin_bottom, 1)


@dataclass(frozen=True)
class DiagramWindow:
    """The visible data window: ``x_min``/``x_max`` on the color (BP-RP) axis
    and ``y_min``/``y_max`` on the magnitude axis.

    ``y_min`` is the BRIGHTER end (smaller magnitude) and is drawn at the TOP —
    the magnitude axis is inverted (brighter up), matching the photometry
    results view. ``x_max > x_min`` and ``y_max > y_min`` are enforced.
    """

    x_min: float
    x_max: float
    y_min: float
    y_max: float

    def __post_init__(self) -> None:
        for name in ("x_min", "x_max", "y_min", "y_max"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HrError(f"{name} must be a number, got {value!r}")
            if not math.isfinite(float(value)):
                raise HrError(f"{name} must be finite, got {value!r}")
        if not self.x_max > self.x_min:
            raise HrError(f"x_max must be > x_min, got [{self.x_min}, {self.x_max}]")
        if not self.y_max > self.y_min:
            raise HrError(f"y_max must be > y_min, got [{self.y_min}, {self.y_max}]")

    @property
    def x_span(self) -> float:
        """The visible color span."""
        return self.x_max - self.x_min

    @property
    def y_span(self) -> float:
        """The visible magnitude span."""
        return self.y_max - self.y_min


#: The default window when there are no points (the placeholder is shown then,
#: but reset/zoom must still not crash).
DEFAULT_WINDOW = DiagramWindow(x_min=-1.0, x_max=5.0, y_min=8.0, y_max=22.0)


def full_window(kind: Any, points: Any) -> DiagramWindow:
    """The full data window of ``points`` on the ``kind`` page (the reset
    view): the color range and the magnitude range of the points, each padded
    by 5% on both sides (degenerate ranges expanded by ±0.05 mag / ±0.1 color).

    Empty ``points`` yield :data:`DEFAULT_WINDOW`.
    """
    kind = _check_kind(kind)
    points = tuple(points)
    if not points:
        return DEFAULT_WINDOW
    coords = [point_xy(kind, p) for p in points]
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    if x_max - x_min < 1e-9:
        x_min, x_max = x_min - 0.1, x_max + 0.1
    else:
        pad = 0.05 * (x_max - x_min)
        x_min, x_max = x_min - pad, x_max + pad
    if y_max - y_min < 1e-6:
        y_min, y_max = y_min - 0.05, y_max + 0.05
    else:
        pad = 0.05 * (y_max - y_min)
        y_min, y_max = y_min - pad, y_max + pad
    return DiagramWindow(x_min=x_min, x_max=x_max, y_min=y_min, y_max=y_max)


def data_to_screen(
    window: DiagramWindow, geom: DiagramGeometry, x: float, y: float
) -> tuple[float, float]:
    """Map a data point ``(x, y)`` to plot pixel coordinates ``(px, py)``.

    The magnitude axis is INVERTED: brighter (smaller ``y``) maps to a smaller
    ``py`` (the top of the plot) — ``window.y_min`` is drawn at
    ``geom.margin_top``.
    """
    px = geom.margin_left + (x - window.x_min) / window.x_span * geom.plot_width
    py = geom.margin_top + (y - window.y_min) / window.y_span * geom.plot_height
    return px, py


def screen_to_data(
    window: DiagramWindow, geom: DiagramGeometry, px: float, py: float
) -> tuple[float, float]:
    """Map plot pixel coordinates ``(px, py)`` back to data ``(x, y)`` — the
    exact inverse of :func:`data_to_screen`."""
    x = window.x_min + (px - geom.margin_left) / geom.plot_width * window.x_span
    y = window.y_min + (py - geom.margin_top) / geom.plot_height * window.y_span
    return x, y


def zoom_window_at(
    window: DiagramWindow, geom: DiagramGeometry, px: float, py: float, factor: float
) -> DiagramWindow:
    """Zoom ``window`` around the cursor at pixel ``(px, py)`` by ``factor``
    (``factor > 1`` zooms IN — the window shrinks; ``factor < 1`` zooms out).

    The data point under the cursor STAYS under the cursor (the zoom is
    centered on it). The spans are clamped to :data:`MIN_X_SPAN` /
    :data:`MIN_Y_SPAN`. ``factor`` must be a positive finite number.
    """
    if isinstance(factor, bool) or not isinstance(factor, (int, float)):
        raise TypeError(f"factor must be a number, got {factor!r}")
    factor = float(factor)
    if not math.isfinite(factor) or factor <= 0:
        raise HrError(f"factor must be positive and finite, got {factor!r}")
    cx, cy = screen_to_data(window, geom, px, py)
    x_span = max(window.x_span / factor, MIN_X_SPAN)
    y_span = max(window.y_span / factor, MIN_Y_SPAN)
    frac_x = (px - geom.margin_left) / geom.plot_width
    frac_y = (py - geom.margin_top) / geom.plot_height
    x_min = cx - frac_x * x_span
    y_min = cy - frac_y * y_span
    return DiagramWindow(
        x_min=x_min, x_max=x_min + x_span, y_min=y_min, y_max=y_min + y_span
    )


def pan_window(
    window: DiagramWindow, geom: DiagramGeometry, dx_px: float, dy_px: float
) -> DiagramWindow:
    """Pan ``window`` by the pixel delta ``(dx_px, dy_px)`` (the content
    follows the cursor): the data window shifts by the opposite data delta."""
    x_per_px = window.x_span / geom.plot_width
    y_per_px = window.y_span / geom.plot_height
    x_min = window.x_min + dx_px * x_per_px
    y_min = window.y_min + dy_px * y_per_px
    return DiagramWindow(
        x_min=x_min,
        x_max=x_min + window.x_span,
        y_min=y_min,
        y_max=y_min + window.y_span,
    )


def point_at(
    window: DiagramWindow,
    geom: DiagramGeometry,
    kind: Any,
    points: Any,
    px: float,
    py: float,
    radius: float = POINT_HIT_RADIUS_PX,
) -> HrPoint | None:
    """The point within ``radius`` pixels of the plot pixel ``(px, py)`` (the
    closest one), or None — the hit-test of the click-to-highlight gesture on
    the diagram."""
    kind = _check_kind(kind)
    best: HrPoint | None = None
    best_dist = float(radius)
    for point in tuple(points):
        try:
            x, y = point_xy(kind, point)
        except HrError:
            continue  # no position on this page (e.g. no abs_g on the H-R page)
        sx, sy = data_to_screen(window, geom, x, y)
        dist = math.hypot(sx - px, sy - py)
        if dist <= best_dist:
            best_dist = dist
            best = point
    return best


def nearest_point_by_pixel(
    points: Any, x: float, y: float, radius: float = STACK_HIT_RADIUS_PX
) -> HrPoint | None:
    """The point whose crossmatch pixel position ``(HrPoint.x, HrPoint.y)``
    is within ``radius`` IMAGE pixels of ``(x, y)`` (the closest one), or None
    — the hit-test of the click gesture on the stack image (the image is
    rendered at the same scale as the coordinates passed here)."""
    best: HrPoint | None = None
    best_dist = float(radius)
    for point in tuple(points):
        dist = math.hypot(point.x - x, point.y - y)
        if dist <= best_dist:
            best_dist = dist
            best = point
    return best


# --------------------------------------------------------------------------------------
# The diagram render (matplotlib Agg -> PNG bytes)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DiagramModel:
    """One frozen diagram page to render.

    - ``kind`` — :data:`CMD` or :data:`HR`;
    - ``points`` — the :class:`~.core.HrPoint`\\ s to plot (members in blue,
      field stars in grey);
    - ``overlay`` — the isochrone overlay points (drawn dashed red; apparent
      ``g`` on the CMD page, ``abs_g`` on the H-R page);
    - ``show_errors`` — draw the parallax-propagated magnitude error bars
      (:func:`abs_g_error`) on the magnitude axis;
    - ``highlighted`` — the ``source_id`` of the highlighted star (drawn with
      a ring), else None;
    - ``title`` — the plot title.
    """

    kind: str
    points: tuple[HrPoint, ...]
    overlay: tuple[IsochronePoint, ...] = ()
    show_errors: bool = False
    highlighted: int | None = None
    title: str = ""

    def __post_init__(self) -> None:
        _check_kind(self.kind)
        if isinstance(self.highlighted, bool) or (
            self.highlighted is not None and not isinstance(self.highlighted, int)
        ):
            raise TypeError(f"highlighted must be an int or None, got {self.highlighted!r}")
        if not isinstance(self.show_errors, bool):
            raise TypeError(f"show_errors must be a bool, got {self.show_errors!r}")


def _check_px(value: Any, name: str) -> int:
    """Validate a render dimension (pixels); raises :class:`HrError`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HrError(f"{name} must be a number of pixels, got {value!r}")
    px = float(value)
    if not math.isfinite(px) or not 1 <= px <= MAX_RENDER_PX:
        raise HrError(f"{name} must be in [1, {MAX_RENDER_PX}] pixels, got {value!r}")
    return int(px)


def render_diagram_png(
    model: DiagramModel,
    window: DiagramWindow,
    *,
    width_px: int = DEFAULT_WIDTH_PX,
    height_px: int = DEFAULT_HEIGHT_PX,
    dpi: int = DEFAULT_DPI,
) -> bytes:
    """Render ``model`` (one diagram page) to PNG bytes (matplotlib Agg, no
    display needed).

    - ``width_px`` / ``height_px`` / ``dpi`` — the render size (the figure is
      ``width_px / dpi`` × ``height_px / dpi`` inches at ``dpi``, so the PNG
      comes out exactly ``width_px`` × ``height_px`` pixels). Values outside
      ``[1, MAX_RENDER_PX]`` raise :class:`HrError`.

    The axes rectangle is set (via ``subplots_adjust``) to EXACTLY the
    :class:`DiagramGeometry` inner plot rectangle of the same size, so
    :func:`data_to_screen` agrees with matplotlib's transform pixel-for-pixel.
    The magnitude axis is inverted (brighter at the top). Returns the PNG file
    contents as ``bytes``.
    """
    if not isinstance(model, DiagramModel):
        raise TypeError(f"model must be a DiagramModel, got {model!r}")
    if not isinstance(window, DiagramWindow):
        raise TypeError(f"window must be a DiagramWindow, got {window!r}")
    width_px = _check_px(width_px, "width_px")
    height_px = _check_px(height_px, "height_px")
    dpi = _check_px(dpi, "dpi")
    geom = DiagramGeometry(width_px, height_px)

    fig = Figure(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
    FigureCanvasAgg(fig)  # attach the Agg canvas (print_png works headless)
    ax = fig.add_subplot(1, 1, 1)
    # the axes rectangle == the DiagramGeometry inner plot rectangle (pixels)
    fig.subplots_adjust(
        left=geom.margin_left / width_px,
        right=(width_px - geom.margin_right) / width_px,
        bottom=geom.margin_bottom / height_px,
        top=(height_px - geom.margin_top) / height_px,
    )

    ax.set_xlim(window.x_min, window.x_max)
    ax.set_ylim(window.y_max, window.y_min)  # inverted: brighter (y_min) at the top
    ax.set_xlabel("BP − RP (mag)")
    ax.set_ylabel("G (mag)" if model.kind == CMD else "M_G (mag)")
    if model.title:
        ax.set_title(model.title, fontsize=9)
    ax.tick_params(labelsize=7)

    # the isochrone overlay (behind the points): apparent g on CMD, abs_g on HR
    if model.overlay:
        ox = [p.bp_rp for p in model.overlay]
        oy = [p.g if model.kind == CMD else p.abs_g for p in model.overlay]
        ax.plot(
            ox,
            oy,
            color=OVERLAY_COLOR,
            linewidth=1.4,
            linestyle="--",
            label="isochrone",
            zorder=1,
        )

    # the stars: members blue, field stars grey; error bars + highlight ring
    members = [p for p in model.points if p.member]
    field = [p for p in model.points if not p.member]
    for group, color, label in (
        (members, MEMBER_COLOR, "members"),
        (field, FIELD_COLOR, "field"),
    ):
        if not group:
            continue
        xs: list[float] = []
        ys: list[float] = []
        yerr: list[float] = []
        for point in group:
            try:
                x, y = point_xy(model.kind, point)
            except HrError:
                continue
            xs.append(x)
            ys.append(y)
            err = abs_g_error(point) if model.show_errors else None
            yerr.append(err if err is not None else 0.0)
        if model.show_errors:
            ax.errorbar(
                xs,
                ys,
                yerr=yerr,
                fmt="o",
                color=color,
                ecolor=color,
                elinewidth=0.7,
                capsize=0,
                markersize=4,
                alpha=0.9,
                label=label,
                zorder=2,
            )
        else:
            ax.plot(xs, ys, "o", color=color, markersize=4, alpha=0.9, label=label, zorder=2)

    # the highlighted star: a ring on top
    if model.highlighted is not None:
        for point in model.points:
            if point.source_id != model.highlighted:
                continue
            try:
                x, y = point_xy(model.kind, point)
            except HrError:
                continue
            ax.plot(
                [x],
                [y],
                "o",
                color=HIGHLIGHT_COLOR,
                markersize=11,
                markerfacecolor="none",
                markeredgewidth=2.0,
                zorder=3,
            )
            break

    if model.overlay or model.points:
        ax.legend(loc="upper right", fontsize=7, framealpha=0.9)

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png")
    return buffer.getvalue()


# --------------------------------------------------------------------------------------
# The stack image render (PIL: the cross-match markers + the highlight ring)
# --------------------------------------------------------------------------------------


def render_stack_png(
    image: Image.Image,
    points: Sequence[HrPoint],
    highlighted: int | None = None,
    *,
    max_size: int = DEFAULT_STACK_MAX_PX,
) -> bytes:
    """Render the stack ``image`` (a PIL image) with the cross-match markers
    drawn at the table's pixel positions, to PNG bytes.

    - ``points`` — the :class:`~.core.HrPoint`\\ s (their ``x`` / ``y`` are the
      0-based detection pixel positions); every point gets a small filled
      marker, the ``highlighted`` ``source_id`` additionally a ring;
    - ``max_size`` — the image is downscaled to at most this many pixels on
      its long side BEFORE the markers are drawn (markers stay crisp; the
      point coordinates are scaled by the same factor).

    Returns the PNG file contents as ``bytes``.
    """
    if not isinstance(image, Image.Image):
        raise TypeError(f"image must be a PIL.Image.Image, got {image!r}")
    if isinstance(max_size, bool) or not isinstance(max_size, int) or max_size < 1:
        raise HrError(f"max_size must be a positive int, got {max_size!r}")
    rgb = image.convert("RGB")
    scale = 1.0
    if max(rgb.size) > max_size:
        scale = max_size / float(max(rgb.size))
        new_size = (max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale)))
        rgb = rgb.resize(new_size, Image.Resampling.BILINEAR)
    draw = ImageDraw.Draw(rgb)
    for point in points:
        px, py = point.x * scale, point.y * scale
        r = 3.0 * scale
        if point.source_id == highlighted:
            rr = 8.0 * scale
            draw.ellipse(
                (px - rr, py - rr, px + rr, py + rr),
                outline=STACK_HIGHLIGHT_COLOR,
                width=max(1, round(2.0 * scale)),
            )
        draw.ellipse((px - r, py - r, px + r, py + r), fill=STACK_MARKER_COLOR)
    buffer = io.BytesIO()
    rgb.save(buffer, format="PNG")
    return buffer.getvalue()


def stack_image_scale(image_size: tuple[int, int], max_size: int = DEFAULT_STACK_MAX_PX) -> float:
    """The downscale factor :func:`render_stack_png` applies to an image of
    ``image_size`` (``(width, height)``) with the given ``max_size`` — the
    factor the widget uses to map click coordinates on the displayed image
    back to the crossmatch table's pixel positions."""
    if isinstance(max_size, bool) or not isinstance(max_size, int) or max_size < 1:
        raise HrError(f"max_size must be a positive int, got {max_size!r}")
    width, height = image_size
    if width <= 0 or height <= 0:
        raise HrError(f"image_size must be positive, got {image_size!r}")
    if max(width, height) > max_size:
        return max_size / float(max(width, height))
    return 1.0


__all__ = [
    "CMD",
    "DEFAULT_DPI",
    "DEFAULT_HEIGHT_PX",
    "DEFAULT_STACK_MAX_PX",
    "DEFAULT_WIDTH_PX",
    "DEFAULT_WINDOW",
    "FIELD_COLOR",
    "HIGHLIGHT_COLOR",
    "HR",
    "KINDS",
    "MAX_RENDER_PX",
    "MEMBER_COLOR",
    "MIN_X_SPAN",
    "MIN_Y_SPAN",
    "OVERLAY_COLOR",
    "PLOT_MARGIN_BOTTOM",
    "PLOT_MARGIN_LEFT",
    "PLOT_MARGIN_RIGHT",
    "PLOT_MARGIN_TOP",
    "POINT_HIT_RADIUS_PX",
    "STACK_HIGHLIGHT_COLOR",
    "STACK_HIT_RADIUS_PX",
    "STACK_MARKER_COLOR",
    "ZOOM_FACTOR",
    "DiagramGeometry",
    "DiagramModel",
    "DiagramWindow",
    "abs_g_error",
    "data_to_screen",
    "full_window",
    "nearest_point_by_pixel",
    "pan_window",
    "point_at",
    "point_xy",
    "render_diagram_png",
    "render_stack_png",
    "screen_to_data",
    "stack_image_scale",
    "zoom_window_at",
]
