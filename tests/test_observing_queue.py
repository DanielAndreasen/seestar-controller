"""Pure-Python plan-queue tests — no gi, no network, no link (observing 01).

Covers the pure layer of ``seestar_controller.observing``:

- :class:`~seestar_controller.observing.queue.QueueEntry` validation — bad
  exposure / gain / mosaic, both name+coord set, neither set, empty or
  non-string name, wrong coord type — plus the valid shapes (by name, by
  coord, mosaic bool / (pan, tilt) / mapping) and ``describe()``;
- :class:`~seestar_controller.observing.queue.PlanQueue` operations — add /
  remove / duplicate / move / move_up / move_down / clear, ``len`` / ``[]`` /
  the ``entries`` snapshot immutability, index errors, and listener
  notifications with :class:`~...queue.QueueEvent`\\ s;
- :func:`~seestar_controller.observing.queue.resolve_names` with a MOCKED
  resolver — known name -> coord filled (name kept), unknown name -> per-entry
  error and the entry stays unresolved, resolver raising -> collected error
  (no crash), duck-typed resolver result normalized, the default resolver
  driven through a monkeypatched catalog ``lookup_name``;
- the RA/Dec parse path via the REAL ``catalog.coords.parse_ra`` /
  ``parse_dec`` / ``parse_coords`` (valid + invalid input);
- :class:`~seestar_controller.observing.queue_controller.PlanQueueController`
  — add_by_name / add_by_coords / add_resolved, editing delegation, listener
  forwarding, ``resolve_name`` preview, ``resolve_names`` with an injected
  resolver;
- the package boundary: a subprocess test (no ``gi``, no ``link``, widget
  layer stays lazy) + a source-scan audit mirroring tests/test_ui_harness.py;
- integration-ish: the queue controller alongside the ``ui_harness`` fixture
  (the app-level controllers with fakes), driven headless.

The widget tests live in tests/test_observing_widgets.py and skip without
``gi``.
"""

import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import seestar_controller.observing as observing
from seestar_controller.catalog.coords import (
    CoordinateError,
    J2000Coord,
    parse_coords,
    parse_dec,
    parse_ra,
)
from seestar_controller.catalog.simbad import CatalogCandidate, NameNotFoundError
from seestar_controller.observing import (
    EVENT_ADDED,
    EVENT_CLEARED,
    EVENT_DUPLICATED,
    EVENT_MOVED,
    EVENT_REMOVED,
    EVENT_RESOLVED,
    QUEUE_EVENT_KINDS,
    PlanQueue,
    PlanQueueController,
    QueueEntry,
    QueueError,
    QueueEvent,
    ResolutionReport,
    default_name_resolver,
    describe_mosaic,
    normalize_mosaic,
    parse_entry_coords,
    resolve_names,
)

#: Directory of the observing package (source-scan audit).
OBSERVING_PACKAGE_DIR = Path(observing.__file__).resolve().parent

#: The pure observing modules — no ``gi`` import allowed (subprocess-enforced).
PURE_OBSERVING_MODULES = ("__init__", "queue", "queue_controller")

#: The executor-layer observing modules — no ``gi`` allowed, but they DO import
#: the link + store (that is the point of the executor); exported lazily.
#: ``observe_controller`` (ticket 03) additionally imports the ui connection /
#: notification controllers (pure, gi-free) — still no ``gi``.
EXECUTOR_OBSERVING_MODULES = ("executor", "observe_controller")

#: The widget observing modules — ``gi`` at module top, lazy-exported via PEP 562.
WIDGET_OBSERVING_MODULES = ("queue_editor", "observe_view")

COORD_A = J2000Coord(ra_deg=83.8215, dec_deg=-5.39114)  # ~ M 42 area
COORD_B = J2000Coord(ra_deg=10.68, dec_deg=41.27)  # ~ M 31 area
COORD_C = J2000Coord.from_hours(5.5881, 12.25)


def entry_by_name(name="M 42", **kwargs):
    return QueueEntry(name=name, **kwargs)


def entry_by_coord(coord=COORD_A, **kwargs):
    return QueueEntry(coord=coord, **kwargs)


# --- QueueEntry validation -----------------------------------------------------------


def test_entry_by_name_defaults():
    e = entry_by_name("M 42")
    assert e.name == "M 42" and e.coord is None
    assert e.mosaic is False
    assert e.exposure_s == 10.0 and e.gain == 50
    assert e.is_resolved is False


def test_entry_by_coord_defaults():
    e = entry_by_coord(COORD_B)
    assert e.coord is COORD_B and e.name is None
    assert e.is_resolved is True


def test_entry_name_is_stripped():
    assert entry_by_name("  M 42  ").name == "M 42"


def test_entry_both_name_and_coord_set_raises():
    with pytest.raises(QueueError) as excinfo:
        QueueEntry(name="M 42", coord=COORD_A)
    assert "exactly one" in str(excinfo.value)
    assert isinstance(excinfo.value, ValueError)


def test_entry_neither_name_nor_coord_raises():
    with pytest.raises(QueueError) as excinfo:
        QueueEntry()
    assert "exactly one" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["", "   ", 42, 3.5, ["M 42"]])
def test_entry_bad_name_raises(bad):
    with pytest.raises(QueueError):
        QueueEntry(name=bad)


def test_entry_wrong_coord_type_raises():
    with pytest.raises(QueueError) as excinfo:
        QueueEntry(coord=(83.8, -5.4))
    assert "J2000Coord" in str(excinfo.value)


@pytest.mark.parametrize("bad", [0, 0.5, 60.5, 61, -1, float("nan"), float("inf"), "10", True, None])
def test_entry_bad_exposure_raises(bad):
    with pytest.raises(QueueError) as excinfo:
        entry_by_name(exposure_s=bad)
    assert "exposure_s" in str(excinfo.value)


@pytest.mark.parametrize("good", [1, 1.0, 10, 30.5, 60, 60.0])
def test_entry_exposure_boundaries_ok(good):
    assert entry_by_name(exposure_s=good).exposure_s == float(good)


@pytest.mark.parametrize("bad", [-1, 101, 100.5, float("nan"), "80", False, None])
def test_entry_bad_gain_raises(bad):
    with pytest.raises(QueueError) as excinfo:
        entry_by_name(gain=bad)
    assert "gain" in str(excinfo.value)


@pytest.mark.parametrize("good", [0, 1, 50, 80.0, 100])
def test_entry_gain_boundaries_ok(good):
    e = entry_by_name(gain=good)
    assert e.gain == int(good) and isinstance(e.gain, int)


@pytest.mark.parametrize("bad", [11.0, -0.5, (1, 2, 3), "x", 5, (float("nan"), 0), {"pan": 11, "tilt": 0}])
def test_entry_bad_mosaic_raises(bad):
    with pytest.raises(QueueError) as excinfo:
        entry_by_name(mosaic=bad)
    assert "mosaic" in str(excinfo.value)


@pytest.mark.parametrize(
    "good,expected",
    [
        (False, False),
        (True, True),
        (None, False),
        ((2.0, 1.0), (2.0, 1.0)),
        ([0, 10], (0.0, 10.0)),
        ({"pan": 2, "tilt": 1}, (2.0, 1.0)),
        ((0, 0), (0.0, 0.0)),
    ],
)
def test_entry_mosaic_normalized(good, expected):
    assert entry_by_name(mosaic=good).mosaic == expected


def test_normalize_mosaic_helper():
    assert normalize_mosaic(True) is True
    assert normalize_mosaic((3, 4)) == (3.0, 4.0)
    with pytest.raises(QueueError):
        normalize_mosaic((1, 2, 3))


def test_describe_mosaic():
    assert describe_mosaic(False) == "no mosaic"
    assert describe_mosaic(True) == "mosaic on"
    assert describe_mosaic((2.0, 1.5)) == "mosaic 2°×1.5°"


def test_entry_describe_formats():
    assert entry_by_name("M 42").describe() == "M 42 (unresolved) | no mosaic | 10 s | gain 50"
    resolved = QueueEntry.resolved("M 42", COORD_A, mosaic=(2.0, 1.0), exposure_s=30, gain=80)
    text = resolved.describe()
    assert text.startswith("M 42 (RA 5.5881h Dec -5.3911°) | mosaic 2°×1° | 30 s | gain 80")
    coord_only = entry_by_coord(COORD_C, exposure_s=5, gain=10)
    assert coord_only.describe().startswith("RA 5.5881h Dec +12.2500° | no mosaic | 5 s | gain 10")
    assert str(coord_only) == coord_only.describe()


def test_resolved_entry_carries_name_and_coord():
    e = QueueEntry.resolved("M 42", COORD_A, exposure_s=20, gain=60)
    assert e.name == "M 42" and e.coord is COORD_A and e.is_resolved
    assert e.exposure_s == 20.0 and e.gain == 60
    with pytest.raises(QueueError):
        QueueEntry.resolved("", COORD_A)
    with pytest.raises(QueueError):
        QueueEntry.resolved("M 42", (1, 2))


def test_entry_is_frozen():
    e = entry_by_name("M 42")
    with pytest.raises(Exception):
        e.name = "other"  # type: ignore[misc]


# --- PlanQueue operations + listeners -----------------------------------------------------


def test_queue_add_len_getitem_entries_snapshot():
    q = PlanQueue()
    assert len(q) == 0 and q.entries == ()
    a = q.add(entry_by_name("A"))
    b = q.add(entry_by_coord(COORD_B))
    assert len(q) == 2
    assert q[0] is a and q[1] is b and q[-1] is b
    assert q.entries == (a, b)
    assert isinstance(q.entries, tuple)
    # snapshot immutability: later changes never mutate an earlier snapshot
    snapshot = q.entries
    q.add(entry_by_name("C"))
    assert snapshot == (a, b) and len(q.entries) == 3
    assert list(q) == list(q.entries)  # iterable over a snapshot


def test_queue_constructor_accepts_entries():
    q = PlanQueue([entry_by_name("A"), entry_by_coord(COORD_B)])
    assert len(q) == 2
    with pytest.raises(QueueError):
        PlanQueue(["not an entry"])


def test_queue_add_rejects_non_entry():
    q = PlanQueue()
    with pytest.raises(QueueError):
        q.add("M 42")


@pytest.mark.parametrize("bad", [5, -3, 1.5, "0", True, None])
def test_queue_bad_index_raises(bad):
    q = PlanQueue([entry_by_name("A"), entry_by_name("B")])
    with pytest.raises(QueueError) as excinfo:
        q.remove(bad)
    assert "index" in str(excinfo.value)
    with pytest.raises(QueueError):
        q[bad]


def test_queue_remove():
    q = PlanQueue([entry_by_name("A"), entry_by_name("B"), entry_by_name("C")])
    removed = q.remove(1)
    assert removed.name == "B"
    assert [e.name for e in q.entries] == ["A", "C"]
    assert q.remove(-1).name == "C"
    assert [e.name for e in q.entries] == ["A"]


def test_queue_duplicate_inserts_copy_after_index():
    q = PlanQueue([entry_by_name("A"), entry_by_name("B")])
    seen = []
    q.add_listener(seen.append)
    copy = q.duplicate(0)
    assert copy == q[0] and copy is not q[0]
    assert [e.name for e in q.entries] == ["A", "A", "B"]
    assert seen == [QueueEvent(EVENT_DUPLICATED, index=1, entry=copy)]
    # duplicating a resolved entry keeps name+coord
    q2 = PlanQueue([QueueEntry.resolved("M 42", COORD_A)])
    dup = q2.duplicate(0)
    assert dup.name == "M 42" and dup.coord is COORD_A


def test_queue_move_semantics():
    q = PlanQueue([entry_by_name("A"), entry_by_name("B"), entry_by_name("C")])
    seen = []
    q.add_listener(seen.append)
    q.move(0, 2)
    assert [e.name for e in q.entries] == ["B", "C", "A"]
    assert seen[-1] == QueueEvent(EVENT_MOVED, index=2, entry=q[2], to_index=2)
    q.move(2, 0)
    assert [e.name for e in q.entries] == ["A", "B", "C"]
    q.move(1, 1)  # no-op: no event
    assert len(seen) == 2
    with pytest.raises(QueueError):
        q.move(0, 3)
    with pytest.raises(QueueError):
        q.move(-4, 0)


def test_queue_move_up_down():
    q = PlanQueue([entry_by_name("A"), entry_by_name("B"), entry_by_name("C")])
    seen = []
    q.add_listener(seen.append)
    assert q.move_up(0) is False and seen == []
    assert q.move_up(1) is True
    assert [e.name for e in q.entries] == ["B", "A", "C"]
    assert q.move_down(2) is False and len(seen) == 1
    assert q.move_down(0) is True
    assert [e.name for e in q.entries] == ["A", "B", "C"]
    assert [e.kind for e in seen] == [EVENT_MOVED, EVENT_MOVED]


def test_queue_clear():
    q = PlanQueue([entry_by_name("A"), entry_by_name("B")])
    seen = []
    q.add_listener(seen.append)
    q.clear()
    assert len(q) == 0 and q.entries == ()
    assert seen == [QueueEvent(EVENT_CLEARED)]
    q.clear()  # already empty: no-op, no event
    assert seen == [QueueEvent(EVENT_CLEARED)]


def test_queue_listener_add_remove_idempotent_decorator():
    q = PlanQueue()
    calls_a, calls_b = [], []
    q.add_listener(calls_a.append)
    q.add_listener(calls_b.append)
    q.add_listener(calls_a.append)  # idempotent
    assert len(q.listeners) == 2

    @q.add_listener
    def on_change(event):
        on_change.seen = event

    assert on_change in q.listeners
    q.remove_listener(calls_a.append)
    q.remove_listener(calls_a.append)  # no-op when absent
    assert len(q.listeners) == 2
    q.add(entry_by_name("A"))
    assert len(calls_b) == 1 and calls_b[0].kind == EVENT_ADDED
    assert on_change.seen.kind == EVENT_ADDED


def test_queue_event_kinds_cover_all_ops():
    q = PlanQueue([entry_by_name("A"), entry_by_name("B")])
    seen = []
    q.add_listener(seen.append)
    q.add(entry_by_name("C"))
    q.duplicate(0)
    q.move(0, 2)
    q.remove(1)
    q.clear()
    assert [e.kind for e in seen] == [
        EVENT_ADDED,
        EVENT_DUPLICATED,
        EVENT_MOVED,
        EVENT_REMOVED,
        EVENT_CLEARED,
    ]
    assert set(QUEUE_EVENT_KINDS) == {
        EVENT_ADDED,
        EVENT_REMOVED,
        EVENT_DUPLICATED,
        EVENT_MOVED,
        EVENT_CLEARED,
        EVENT_RESOLVED,
    }


# --- resolve_names with a mocked resolver ------------------------------------------------


def fake_resolver(mapping, *, fail_with=None):
    """A resolver: name -> J2000Coord from ``mapping``; unknown names raise
    NameNotFoundError; ``fail_with`` (an exception) makes EVERY call raise it."""

    def resolver(name):
        if fail_with is not None:
            raise fail_with
        if name not in mapping:
            raise NameNotFoundError(f"SIMBAD has no object named {name!r}")
        return mapping[name]

    return resolver


def test_resolve_names_fills_coord_and_keeps_name():
    q = PlanQueue(
        [entry_by_name("M 42"), entry_by_coord(COORD_B), entry_by_name("NGC 1976")]
    )
    seen = []
    q.add_listener(seen.append)
    report = resolve_names(q, fake_resolver({"M 42": COORD_A, "NGC 1976": COORD_C}))
    assert isinstance(report, ResolutionReport)
    assert report.ok and report.errors == ()
    assert [index for index, _ in report.resolved] == [0, 2]
    e0 = q[0]
    assert e0.name == "M 42" and e0.coord == COORD_A and e0.is_resolved
    assert q[1].coord is COORD_B and q[1].name is None  # coord-only entry untouched
    assert q[2].coord == COORD_C
    # resolution kept the other fields
    q2 = PlanQueue([entry_by_name("M 42", mosaic=(2.0, 1.0), exposure_s=30, gain=80)])
    resolve_names(q2, fake_resolver({"M 42": COORD_A}))
    assert q2[0].mosaic == (2.0, 1.0) and q2[0].exposure_s == 30.0 and q2[0].gain == 80
    # a resolved event per resolved entry
    assert [e.kind for e in seen] == [EVENT_RESOLVED, EVENT_RESOLVED]
    assert seen[0].index == 0 and seen[0].entry is q[0]


def test_resolve_names_unknown_name_collected_entry_stays_unresolved():
    q = PlanQueue([entry_by_name("M 42"), entry_by_name("NO SUCH OBJECT")])
    report = resolve_names(q, fake_resolver({"M 42": COORD_A}))
    assert not report.ok
    assert len(report.resolved) == 1 and report.resolved[0][0] == 0
    assert len(report.errors) == 1
    index, message = report.errors[0]
    assert index == 1 and "NO SUCH OBJECT" in message and "NameNotFoundError" in message
    # the failed entry keeps its name and is still unresolved — no raise
    assert q[1].name == "NO SUCH OBJECT" and q[1].coord is None and not q[1].is_resolved
    assert q[0].coord == COORD_A


def test_resolve_names_resolver_raising_is_collected_not_crash():
    q = PlanQueue([entry_by_name("A"), entry_by_name("B")])
    report = resolve_names(q, fake_resolver({}, fail_with=RuntimeError("catalog down")))
    assert report.resolved == ()
    assert len(report.errors) == 2
    assert all("catalog down" in message for _, message in report.errors)
    assert all(e.coord is None for e in q.entries)  # nothing resolved, nothing raised


def test_resolve_names_duck_typed_resolver_result_normalized():
    q = PlanQueue([entry_by_name("M 42")])
    candidate = CatalogCandidate(name="M 42", ra_deg=83.8215, dec_deg=-5.39114)
    report = resolve_names(q, lambda name: candidate)  # has ra_deg/dec_deg
    assert report.ok
    assert q[0].coord == COORD_A


def test_resolve_names_bad_resolver_result_collected():
    q = PlanQueue([entry_by_name("M 42")])
    report = resolve_names(q, lambda name: None)
    assert report.resolved == () and len(report.errors) == 1
    assert "J2000Coord" in report.errors[0][1]
    assert q[0].coord is None


def test_resolve_names_rejects_non_queue():
    with pytest.raises(QueueError):
        resolve_names([entry_by_name("A")])


def test_resolve_names_default_resolver_uses_catalog_lookup_name(monkeypatch):
    """The default resolver is the lazy catalog facade wrapper: monkeypatch
    ``catalog.facade.lookup_name`` and resolve without injecting a resolver."""
    import seestar_controller.catalog.facade as facade

    calls = []

    def fake_lookup_name(name, cache=None, **kwargs):
        calls.append(name)
        return [CatalogCandidate(name=name, ra_deg=83.8215, dec_deg=-5.39114, magnitude=4.8)]

    monkeypatch.setattr(facade, "lookup_name", fake_lookup_name)
    q = PlanQueue([entry_by_name("M 42")])
    report = resolve_names(q)  # default resolver
    assert report.ok and calls == ["M 42"]
    assert q[0].coord == COORD_A

    # a catalog failure (NameNotFoundError) is collected per entry, not raised
    def raising_lookup_name(name, cache=None, **kwargs):
        raise NameNotFoundError(f"SIMBAD has no object named {name!r}")

    monkeypatch.setattr(facade, "lookup_name", raising_lookup_name)
    q2 = PlanQueue([entry_by_name("NOPE")])
    report2 = resolve_names(q2)
    assert report2.resolved == () and len(report2.errors) == 1
    assert q2[0].coord is None


def test_default_name_resolver_brightest_first(monkeypatch):
    import seestar_controller.catalog.facade as facade

    monkeypatch.setattr(
        facade,
        "lookup_name",
        lambda name, cache=None, **kw: [
            CatalogCandidate(name="bright", ra_deg=1.0, dec_deg=2.0, magnitude=3.0),
            CatalogCandidate(name="dim", ra_deg=10.0, dec_deg=20.0, magnitude=12.0),
        ],
    )
    assert default_name_resolver("M 42") == J2000Coord(ra_deg=1.0, dec_deg=2.0)


# --- RA/Dec parse path via the real catalog.coords -----------------------------------------


def test_parse_ra_dec_valid_forms():
    assert parse_ra("5.5881") == pytest.approx(5.5881 * 15.0)
    assert parse_ra("12h34m56.7s") == pytest.approx((12 + 34 / 60 + 56.7 / 3600) * 15.0)
    assert parse_ra("12:34:56.7") == pytest.approx(parse_ra("12h34m56.7s"))
    assert parse_ra("25") == pytest.approx(15.0)  # RA wraps modulo 24 h
    assert parse_dec("-5.39114") == pytest.approx(-5.39114)
    assert parse_dec("-05:23:28") == pytest.approx(-(5 + 23 / 60 + 28 / 3600))
    assert parse_dec("-05°23′28″") == pytest.approx(parse_dec("-05:23:28"))
    coord = parse_coords("5h35m17.3s", "-05°23′28″")
    assert isinstance(coord, J2000Coord)
    assert coord.ra_deg == pytest.approx((5 + 35 / 60 + 17.3 / 3600) * 15.0)
    assert coord.dec_deg == pytest.approx(-(5 + 23 / 60 + 28 / 3600))


@pytest.mark.parametrize(
    "ra,dec",
    [("abc", "0"), ("5.5", ""), ("-5", "0"), ("12h61m", "0"), ("5.5", "91"), ("5.5", "-90.5"), (None, 0), ("5.5", "xyz")],
)
def test_parse_ra_dec_invalid_raises_coordinate_error(ra, dec):
    with pytest.raises(CoordinateError) as excinfo:
        parse_coords(ra, dec)
    assert isinstance(excinfo.value, ValueError)


def test_parse_entry_coords_wraps_coordinate_error():
    coord = parse_entry_coords("5.5881", "-5.39114")
    assert coord.ra_deg == pytest.approx(83.8215) and coord.dec_deg == pytest.approx(-5.39114)
    with pytest.raises(QueueError) as excinfo:
        parse_entry_coords("abc", "0")
    assert "invalid coordinates" in str(excinfo.value)
    assert isinstance(excinfo.value, ValueError)


# --- PlanQueueController ---------------------------------------------------------------------


@pytest.fixture()
def resolver():
    return fake_resolver({"M 42": COORD_A, "M 31": COORD_B})


@pytest.fixture()
def controller(resolver):
    return PlanQueueController(resolver=resolver)


def test_controller_defaults_and_accessors(controller):
    assert isinstance(controller.queue, PlanQueue)
    assert controller.entries == () and len(controller) == 0
    assert controller.listeners == ()
    assert callable(controller.resolver)


def test_controller_add_by_name(controller):
    e = controller.add_by_name("M 42", exposure_s=20, gain=80, mosaic=(1.0, 2.0))
    assert e.name == "M 42" and e.coord is None
    assert e.exposure_s == 20.0 and e.gain == 80 and e.mosaic == (1.0, 2.0)
    assert controller.entries == (e,)
    assert controller[0] is e
    with pytest.raises(QueueError):
        controller.add_by_name("   ")


def test_controller_add_by_coords(controller):
    e = controller.add_by_coords("5.5881", "-5.39114", exposure_s=5, gain=10)
    assert e.coord is not None
    assert e.coord.ra_deg == pytest.approx(83.8215) and e.coord.dec_deg == pytest.approx(-5.39114)
    assert e.name is None
    # sexagesimal works too
    e2 = controller.add_by_coords("12h34m56.7s", "-05:23:28")
    assert e2.coord.ra_deg == pytest.approx(parse_ra("12h34m56.7s"))
    # invalid input -> QueueError (wrapping the catalog's CoordinateError)
    with pytest.raises(QueueError) as excinfo:
        controller.add_by_coords("abc", "0")
    assert "invalid coordinates" in str(excinfo.value)
    with pytest.raises(QueueError):
        controller.add_by_coords("5.5", "91")
    assert len(controller) == 2


def test_controller_add_resolved(controller):
    e = controller.add_resolved("M 42", COORD_A)
    assert e.name == "M 42" and e.coord is COORD_A
    with pytest.raises(QueueError):
        controller.add_resolved("M 42", "not a coord")


def test_controller_editing_delegates_and_forwards_events(controller):
    seen = []
    controller.add_listener(seen.append)
    controller.add_by_name("A")
    controller.add_by_name("B")
    controller.add_by_name("C")
    assert controller.move_up(1) is True
    assert [e.name for e in controller.entries] == ["B", "A", "C"]
    controller.move(0, 2)
    assert [e.name for e in controller.entries] == ["A", "C", "B"]
    dup = controller.duplicate(0)
    assert controller.entries[1] == dup
    removed = controller.remove(1)
    assert removed == dup
    assert controller.move_down(2) is False
    controller.clear()
    assert len(controller) == 0
    assert [e.kind for e in seen] == [
        EVENT_ADDED,
        EVENT_ADDED,
        EVENT_ADDED,
        EVENT_MOVED,
        EVENT_MOVED,
        EVENT_DUPLICATED,
        EVENT_REMOVED,
        EVENT_CLEARED,
    ]
    # listener management on the controller
    other = []
    controller.add_listener(other.append)
    controller.add_listener(other.append)  # idempotent
    assert len(controller.listeners) == 2
    controller.remove_listener(seen.append)
    controller.remove_listener(seen.append)  # no-op
    assert controller.listeners == (other.append,)


def test_controller_resolve_name_preview(controller):
    coord = controller.resolve_name("M 42")
    assert coord == COORD_A
    with pytest.raises(QueueError) as excinfo:
        controller.resolve_name("NOPE")
    assert "NOPE" in str(excinfo.value) and "NameNotFoundError" in str(excinfo.value)
    with pytest.raises(QueueError):
        controller.resolve_name("   ")


def test_controller_resolve_names_uses_injected_resolver(controller):
    controller.add_by_name("M 42")
    controller.add_by_coords("1", "2")
    controller.add_by_name("UNKNOWN")
    report = controller.resolve_names()
    assert [i for i, _ in report.resolved] == [0]
    assert controller[0].coord == COORD_A and controller[0].name == "M 42"
    assert controller[1].coord is not None and controller[1].name is None
    assert controller[2].coord is None and len(report.errors) == 1
    # resolving again is a no-op for already-resolved entries
    report2 = controller.resolve_names()
    assert report2.resolved == () and len(report2.errors) == 1


def test_controller_wraps_a_given_queue(resolver):
    q = PlanQueue([entry_by_name("M 42")])
    c = PlanQueueController(q, resolver=resolver)
    assert c.queue is q
    c.resolve_names()
    assert q[0].coord == COORD_A


def test_controller_default_resolver_is_the_catalog_wrapper():
    c = PlanQueueController()
    assert c.resolver is default_name_resolver


# --- package boundary (subprocess + source scan) ----------------------------------------------


def test_pure_observing_modules_import_no_gi_no_link_and_widget_stays_lazy():
    imports = "".join(
        f"import seestar_controller.observing.{name};" for name in PURE_OBSERVING_MODULES[1:]
    )
    code = (
        "import sys;"
        "import seestar_controller.observing;"
        f"{imports}"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported eagerly';"
        "assert 'seestar_controller.ui' not in sys.modules, 'ui imported eagerly';"
        "assert 'seestar_controller.observing.queue_editor' not in sys.modules, "
        "'queue_editor imported eagerly';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_every_observing_module_is_pure_or_executor_or_widget_and_lazy_exported():
    modules = {path.stem for path in OBSERVING_PACKAGE_DIR.glob("*.py")}
    assert modules == (
        set(PURE_OBSERVING_MODULES) | set(EXECUTOR_OBSERVING_MODULES) | set(WIDGET_OBSERVING_MODULES)
    )
    for name in PURE_OBSERVING_MODULES:
        source = (OBSERVING_PACKAGE_DIR / f"{name}.py").read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), (
            f"pure module {name} imports gi"
        )
        assert "seestar_controller.link" not in source, f"pure module {name} references link"
    for name in EXECUTOR_OBSERVING_MODULES:
        source = (OBSERVING_PACKAGE_DIR / f"{name}.py").read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE), (
            f"executor module {name} imports gi"
        )
    for name in WIDGET_OBSERVING_MODULES:
        lines = (OBSERVING_PACKAGE_DIR / f"{name}.py").read_text(encoding="utf-8").splitlines()
        gi_lines = [i for i, line in enumerate(lines) if line.strip() == "import gi"]
        assert gi_lines, f"widget module {name} has no 'import gi' at module top"
        repo_line = next(
            i for i, line in enumerate(lines) if line.startswith("from gi.repository")
        )
        assert gi_lines[0] < repo_line, (
            f"widget module {name}: 'import gi' must come before 'from gi.repository'"
        )
    assert set(observing._WIDGET_EXPORTS.values()) == set(WIDGET_OBSERVING_MODULES)
    assert set(observing._EXECUTOR_EXPORTS.values()) == set(EXECUTOR_OBSERVING_MODULES)


def test_public_api_is_exported_from_package():
    for name in (
        "QueueEntry",
        "PlanQueue",
        "PlanQueueController",
        "QueueError",
        "QueueEvent",
        "QueueListener",
        "ResolutionReport",
        "NameResolver",
        "Mosaic",
        "resolve_names",
        "default_name_resolver",
        "parse_entry_coords",
        "normalize_mosaic",
        "describe_mosaic",
        "EVENT_ADDED",
        "EVENT_REMOVED",
        "EVENT_DUPLICATED",
        "EVENT_MOVED",
        "EVENT_CLEARED",
        "EVENT_RESOLVED",
        "QUEUE_EVENT_KINDS",
        "EXPOSURE_MIN_S",
        "EXPOSURE_MAX_S",
        "GAIN_MIN",
        "GAIN_MAX",
        "MOSAIC_MIN_DEG",
        "MOSAIC_MAX_DEG",
        "DEFAULT_EXPOSURE_S",
        "DEFAULT_GAIN",
    ):
        assert name in observing.__all__, name
        assert hasattr(observing, name), name
    assert "QueueEditor" in observing.__all__ and "QueueEditor" in observing._WIDGET_EXPORTS
    assert observing.QueueEntry is QueueEntry
    assert observing.PlanQueueController is PlanQueueController
    assert observing.resolve_names is resolve_names
    assert issubclass(observing.QueueError, ValueError)
    # ranges mirror the link's stacking contract
    from seestar_controller.link import stacking

    assert observing.EXPOSURE_MIN_S == stacking.EXPOSURE_MIN_S
    assert observing.EXPOSURE_MAX_S == stacking.EXPOSURE_MAX_S
    assert observing.GAIN_MIN == stacking.GAIN_MIN
    assert observing.GAIN_MAX == stacking.GAIN_MAX
    assert observing.MOSAIC_MIN_DEG == stacking.MOSAIC_MIN_DEG
    assert observing.MOSAIC_MAX_DEG == stacking.MOSAIC_MAX_DEG


# --- integration-ish: the queue controller alongside the ui_harness fixture --------------------


def test_queue_controller_with_ui_harness(ui_harness):
    """The app-level wiring: the shared PlanQueueController sits next to the
    ui_harness controllers (like SeestarApplication owns it) and drives the
    queue headless with a fake resolver."""
    queue_controller = PlanQueueController(
        resolver=fake_resolver({"M 42": COORD_A, "V* bet Per": COORD_C})
    )
    seen = []
    queue_controller.add_listener(seen.append)

    queue_controller.add_by_name("M 42", exposure_s=30, gain=80)
    queue_controller.add_by_coords("5h35m17s", "+12°15′00″", mosaic=(2.0, 2.0))
    queue_controller.add_by_name("V* bet Per")
    assert len(queue_controller) == 3
    assert ui_harness.connection.status.value == "disconnected"  # untouched

    report = queue_controller.resolve_names()
    assert [i for i, _ in report.resolved] == [0, 2] and report.errors == ()
    assert queue_controller[0].coord == COORD_A
    assert queue_controller[2].coord == COORD_C
    assert queue_controller[1].coord is not None  # added by coords already

    queue_controller.move_up(2)
    assert [e.name for e in queue_controller.entries] == ["M 42", "V* bet Per", None]
    queue_controller.duplicate(0)
    queue_controller.remove(1)
    assert len(queue_controller) == 3
    assert [e.kind for e in seen] == [
        EVENT_ADDED,
        EVENT_ADDED,
        EVENT_ADDED,
        EVENT_RESOLVED,
        EVENT_RESOLVED,
        EVENT_MOVED,
        EVENT_DUPLICATED,
        EVENT_REMOVED,
    ]
