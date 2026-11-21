"""Pure visibility-planner ticket-02 tests — the timeline model + Agg render.

No gi, no network (matplotlib Agg + the planner's builtin ephemeris/bundled
IERS-B). Covers:

- :func:`~seestar_controller.visibility.build_timeline` — the
  :class:`~seestar_controller.visibility.TimelineModel` contents (window,
  axis, per-resolved-entry curves + their queue indices, twilight, moon,
  suggested order, labels), the settings-mapping / datetime-night inputs, and
  the validation errors;
- the display helpers :func:`~...axis_hours` / :func:`~...format_local` /
  :func:`~...suggested_labels` and the settings-source resolver
  :func:`~...resolve_settings`;
- :func:`~...reorder_moves` — the pop/insert move sequence that realizes a
  permutation (simulated against a plain list), incl. the invalid-permutation
  errors;
- :func:`~...apply_order` — writing a suggested order back to a real
  observing ``PlanQueueController`` through its public ``move`` API (move
  count, ``moved`` events seen by a listener, idempotence, errors);
- :func:`~seestar_controller.visibility.render_timeline_png` — PNG bytes of
  the right size (PIL-verified), custom sizes, the validation errors, and the
  empty-queue / polar-night edge cases.
"""

import datetime
import io
import random
from types import SimpleNamespace

import astropy.units as u
import numpy as np
import pytest
from astropy.time import Time
from PIL import Image

from seestar_controller.catalog.coords import J2000Coord
from seestar_controller.observing.queue import QueueEntry
from seestar_controller.observing.queue_controller import PlanQueueController
from seestar_controller.visibility import (
    DEFAULT_HEIGHT_PX,
    DEFAULT_STEP_MINUTES,
    DEFAULT_WIDTH_PX,
    MAX_RENDER_PX,
    MAX_TIMELINE_ENTRIES,
    Site,
    TimelineModel,
    VisibilityError,
    altitude_curves,
    apply_order,
    axis_hours,
    build_timeline,
    format_local,
    moon_track,
    night_grid,
    night_window,
    render_timeline_png,
    reorder_moves,
    resolve_settings,
    suggest_order,
    suggested_labels,
    twilight_times,
)

#: The fixed test site: Munich (48.1373 N, 11.5755 E, 520 m, Europe/Berlin).
MUNICH = Site(latitude_deg=48.1373, longitude_deg=11.5755, elevation_m=520.0, timezone="Europe/Berlin")

#: The fixed test night.
NIGHT = datetime.date(2025, 3, 20)

#: Test targets (J2000).
COORD_M42 = J2000Coord(ra_deg=83.8215, dec_deg=-5.39114)
COORD_RA20 = J2000Coord.from_hours(20.0, 20.0)
COORD_RA5 = J2000Coord.from_hours(5.0, 25.0)

#: A settings mapping equivalent to MUNICH (UTC variant for the mapping tests).
MUNICH_SETTINGS_UTC = {
    "site_latitude": "48.1373",
    "site_longitude": "11.5755",
    "site_elevation_m": "520",
    "site_timezone": "UTC",
}

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _entries():
    """A mixed queue: resolved M 42, an unresolved name, RA 20h, RA 5h."""
    return [
        QueueEntry.resolved("M 42", COORD_M42),
        QueueEntry(name="Unresolved target"),
        QueueEntry(coord=COORD_RA20),
        QueueEntry(coord=COORD_RA5),
    ]


# --- build_timeline ---------------------------------------------------------------------------


@pytest.mark.slow
def test_build_timeline_contents():
    entries = _entries()
    model = build_timeline(entries, MUNICH, NIGHT)
    assert isinstance(model, TimelineModel)
    assert model.site == MUNICH
    assert model.night == NIGHT
    # window + axis: local noon -> local noon, x in 0..24 h
    assert model.window == night_window(MUNICH, NIGHT)
    grid = night_grid(MUNICH, NIGHT)
    assert len(model.axis_hours) == len(grid) == int(24 * 60 / DEFAULT_STEP_MINUTES) + 1
    assert model.axis_hours[0] == pytest.approx(0.0)
    assert model.axis_hours[-1] == pytest.approx(24.0)
    assert np.allclose(model.axis_hours, axis_hours(model.window, grid))
    # labels: one per queue entry, in queue order
    assert model.labels == ("M 42", "Unresolved target", str(COORD_RA20), str(COORD_RA5))
    # curves: one per RESOLVED entry, with the queue index it came from
    assert model.curve_indices == (0, 2, 3)
    assert len(model.curves) == 3
    assert [c.name for c in model.curves] == ["M 42", str(COORD_RA20), str(COORD_RA5)]
    resolved_curves = altitude_curves([entries[0], entries[2], entries[3]], MUNICH, NIGHT)
    for curve, ref in zip(model.curves, resolved_curves, strict=True):
        assert np.all(curve.times.jd == ref.times.jd)
        assert np.allclose(curve.alt.to_value(u.deg), ref.alt.to_value(u.deg))
        assert np.allclose(curve.az.to_value(u.deg), ref.az.to_value(u.deg))
        assert curve.transit_time == ref.transit_time
        assert curve.max_alt == ref.max_alt
    # twilight + moon match the planner functions
    tw = twilight_times(MUNICH, NIGHT)
    assert model.twilight == tw
    assert model.twilight.astro_start is not None and model.twilight.astro_end is not None
    moon = moon_track(MUNICH, NIGHT)
    assert np.all(model.moon.times.jd == moon.times.jd)
    assert np.allclose(model.moon.alt.to_value(u.deg), moon.alt.to_value(u.deg))
    assert np.allclose(
        model.moon.phase.to_value(u.dimensionless_unscaled),
        moon.phase.to_value(u.dimensionless_unscaled),
    )
    # suggested order: a permutation of the queue indices, unresolved deferred last
    assert model.order == suggest_order(entries, MUNICH, NIGHT)
    assert sorted(model.order) == [0, 1, 2, 3]
    assert model.order[-1] == 1  # the unresolved entry goes last
    # RA 5h transits before M 42 (RA 5h35m), which transits before RA 20h
    assert model.order[:3] == (3, 0, 2)


def test_build_timeline_accepts_settings_mapping_and_datetime_night():
    model = build_timeline(
        [QueueEntry(coord=COORD_RA5)],
        MUNICH_SETTINGS_UTC,
        datetime.datetime(2025, 3, 20, 23, 30, tzinfo=datetime.UTC),
    )
    assert model.site == Site(48.1373, 11.5755, 520.0, "UTC")
    assert model.night == NIGHT
    assert model.window == night_window(model.site, NIGHT)
    assert len(model.curves) == 1 and model.curve_indices == (0,)
    assert model.order == (0,)


@pytest.mark.slow
def test_build_timeline_empty_and_all_unresolved():
    empty = build_timeline([], MUNICH, NIGHT)
    assert empty.curves == () and empty.curve_indices == () and empty.order == ()
    assert empty.labels == ()
    unresolved = build_timeline([QueueEntry(name="a"), QueueEntry(name="b")], MUNICH, NIGHT)
    assert unresolved.curves == () and unresolved.order == (0, 1)
    assert unresolved.labels == ("a", "b")


def test_build_timeline_validation_errors():
    entries = _entries()
    with pytest.raises(VisibilityError):
        build_timeline(entries, "not a site", NIGHT)  # ty: ignore[invalid-argument-type]
    with pytest.raises(VisibilityError):
        build_timeline(entries, {}, NIGHT)  # no site in the settings
    with pytest.raises(VisibilityError):
        build_timeline(entries, MUNICH, "2025-03-20")
    with pytest.raises(VisibilityError):
        build_timeline(entries, MUNICH, None)
    with pytest.raises(VisibilityError):
        # a duck-typed entry whose coord is not a J2000Coord
        build_timeline([SimpleNamespace(name="X", coord="not a coord")], MUNICH, NIGHT)
    with pytest.raises(VisibilityError):
        build_timeline([QueueEntry(coord=COORD_RA5)] * (MAX_TIMELINE_ENTRIES + 1), MUNICH, NIGHT)
    with pytest.raises(VisibilityError):
        build_timeline(_entries(), MUNICH, NIGHT, step_minutes=0)


# --- display helpers + settings source -----------------------------------------------------------


def test_axis_hours_and_format_local():
    window = night_window(MUNICH, NIGHT)
    grid = night_grid(MUNICH, NIGHT)
    hours = axis_hours(window, grid)
    assert hours[0] == 0.0 and hours[-1] == pytest.approx(24.0)
    assert np.all(np.diff(hours) > 0)
    # 2025-03-20 19:12:08 UTC = 20:12 CET at Munich
    assert format_local(MUNICH, Time("2025-03-20T19:12:08.042", scale="utc")) == "20:12"


def test_suggested_labels():
    model = build_timeline(_entries(), MUNICH, NIGHT)
    labels = suggested_labels(model)
    assert labels == tuple(model.labels[i] for i in model.order)
    assert labels[-1] == "Unresolved target"
    assert labels[:3] == (str(COORD_RA5), "M 42", str(COORD_RA20))


def test_resolve_settings():
    assert resolve_settings(None) is None
    mapping = {"site_latitude": "48.0"}
    assert resolve_settings(mapping) is mapping
    calls = []

    def source():
        calls.append(1)
        return mapping

    assert resolve_settings(source) is mapping
    assert calls == [1]


# --- reorder_moves ------------------------------------------------------------------------------


def _simulate(moves, length):
    """Apply the moves to a plain list with pop/insert (PlanQueue.move semantics)."""
    items = list(range(length))
    for from_index, to_index in moves:
        items.insert(to_index, items.pop(from_index))
    return items


def test_reorder_moves_identity_is_empty():
    assert reorder_moves((), 0) == ()
    assert reorder_moves((0, 1, 2, 3), 4) == ()


def test_reorder_moves_realizes_the_permutation():
    rng = random.Random(20250320)
    for length in (1, 2, 3, 4, 5, 8):
        for _ in range(20):
            order = list(range(length))
            rng.shuffle(order)
            moves = reorder_moves(order, length)
            assert _simulate(moves, length) == order
    # a known case: (3, 0, 1, 2) over 4 items is a single move
    assert reorder_moves((3, 0, 1, 2), 4) == ((3, 0),)
    assert _simulate(reorder_moves((3, 0, 1, 2), 4), 4) == [3, 0, 1, 2]
    # reversed
    assert _simulate(reorder_moves((2, 1, 0), 3), 3) == [2, 1, 0]


def test_reorder_moves_rejects_bad_permutations():
    for bad_order, length in (
        ((0, 0), 2),  # duplicate
        ((0, 2), 2),  # out of range
        ((0, 1, 2), 2),  # wrong length
        ((0,), 2),
        ((-1, 0), 2),  # negative
        ((0.5, 1.0), 2),  # non-int
    ):
        with pytest.raises(VisibilityError):
            reorder_moves(bad_order, length)  # ty: ignore[invalid-argument-type]
    for bad_length in (-1, 1.5, "4", None, True):
        with pytest.raises(VisibilityError):
            reorder_moves((0,), bad_length)  # ty: ignore[invalid-argument-type]


# --- apply_order ----------------------------------------------------------------------------------


def test_apply_order_reorders_a_plan_queue_controller():
    controller = PlanQueueController()
    controller.add_resolved("M 42", COORD_M42)
    controller.add_resolved("RA20", COORD_RA20)
    controller.add_entry(QueueEntry(name="Unresolved target"))
    controller.add_resolved("RA5", COORD_RA5)
    assert [e.name for e in controller.entries] == ["M 42", "RA20", "Unresolved target", "RA5"]

    events = []
    controller.add_listener(lambda event: events.append(event))

    entries = controller.entries
    order = suggest_order(entries, MUNICH, NIGHT)
    assert order == (3, 0, 1, 2)

    moves = apply_order(controller, order)
    assert moves == len(reorder_moves(order, len(entries))) == 1
    # the queue now matches the suggested order
    assert [e.name for e in controller.entries] == ["RA5", "M 42", "RA20", "Unresolved target"]
    # the move notified the listeners (a 'moved' event)
    assert any(event.kind == "moved" for event in events)
    # applying again is a no-op
    assert apply_order(controller, suggest_order(controller.entries, MUNICH, NIGHT)) == 0
    assert [e.name for e in controller.entries] == ["RA5", "M 42", "RA20", "Unresolved target"]


def test_apply_order_validation():
    controller = PlanQueueController()
    controller.add_resolved("M 42", COORD_M42)
    with pytest.raises(VisibilityError):
        apply_order(controller, (0, 1))  # wrong length
    with pytest.raises(VisibilityError):
        apply_order(controller, (5,))  # out of range
    with pytest.raises(VisibilityError):
        apply_order(object(), (0,))  # no move/len API
    # empty queue + empty order is fine
    assert apply_order(PlanQueueController(), ()) == 0


# --- render_timeline_png --------------------------------------------------------------------------


def test_render_timeline_png_is_a_png_of_the_requested_size():
    model = build_timeline(_entries(), MUNICH, NIGHT)
    png = render_timeline_png(model)
    assert png[:8] == PNG_MAGIC
    with Image.open(io.BytesIO(png)) as image:
        assert image.size == (DEFAULT_WIDTH_PX, DEFAULT_HEIGHT_PX)
        assert image.format == "PNG"
    # custom size
    png2 = render_timeline_png(model, width_px=640, height_px=320, dpi=100)
    with Image.open(io.BytesIO(png2)) as image:
        assert image.size == (640, 320)


def test_render_timeline_png_validation():
    model = build_timeline(_entries(), MUNICH, NIGHT)
    with pytest.raises(VisibilityError):
        render_timeline_png("not a model")  # ty: ignore[invalid-argument-type]
    for bad in (0, -1, MAX_RENDER_PX + 1, float("nan"), "960", None, True):
        with pytest.raises(VisibilityError):
            render_timeline_png(model, width_px=bad)  # ty: ignore[invalid-argument-type]
        with pytest.raises(VisibilityError):
            render_timeline_png(model, height_px=bad)  # ty: ignore[invalid-argument-type]
        with pytest.raises(VisibilityError):
            render_timeline_png(model, dpi=bad)  # ty: ignore[invalid-argument-type]


@pytest.mark.slow
def test_render_timeline_png_edge_cases():
    # no resolved targets (empty curves) still renders
    empty = build_timeline([QueueEntry(name="Unresolved")], MUNICH, NIGHT)
    assert render_timeline_png(empty)[:8] == PNG_MAGIC
    # polar day: no astronomical twilight (astro_start/astro_end None) still renders
    tromso = Site(69.6492, 18.9553, 0.0, "Europe/Oslo")
    polar = build_timeline([QueueEntry(coord=COORD_RA5)], tromso, datetime.date(2025, 6, 15))
    assert polar.twilight.astro_start is None and polar.twilight.astro_end is None
    assert render_timeline_png(polar)[:8] == PNG_MAGIC
