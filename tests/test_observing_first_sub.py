"""Observing ticket 08 — the first stored sub as the Observe-view preview
fallback (headless, pure layer; plus a widget test that skips without GTK).

The Observe view's right-pane preview is fed by the scope's live preview
stream; when that stream has no frame yet (absent, or not informative — e.g.
solar filter indoors), the pane used to stay on "no preview yet" even though
subs were being stored. Ticket 08 wires the :class:`~seestar_controller.observing.capture.SessionCapture`
frame-stored event (a ``frame`` log event) into the
:class:`~seestar_controller.observing.observe_controller.ObserveController`,
which loads the FIRST stored sub of the running session once — decoded,
percentile-stretched, downscaled (:func:`~seestar_controller.observing.observe_controller.load_first_sub_frame`)
— and carries it in every :class:`~seestar_controller.observing.observe_controller.ObserveViewState`
as ``first_sub_frame``. The view shows it while ``preview_frame`` is None;
the live preview takes over once it streams. The load is marshalled through
``invoke_on_main`` and cached (decoded once per night).

Device doubles, per docs/wiki/assumptions.md (no real scope, user AFK — all
link work is mock-only): a **FakeDevice** with a scripted
``download_frames`` writing real FITS via
:func:`~seestar_controller.link.mock_scope.make_fits_bytes` (the
test_observing_capture shape) and a **FakePreviewStream** that starts but
never emits — the live stream stays idle for the whole test, so anything
shown in the preview area comes from the stored sub. The capture worker is
parked on a long poll interval; the store step is driven synchronously with
:meth:`~seestar_controller.observing.capture.SessionCapture.poll_once`.

The widget test at the bottom uses the same ``pytest.importorskip("gi")`` +
``Gtk.init_check()`` guards as tests/test_observing_widgets.py and skips in
the headless sandbox.
"""

import dataclasses
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from seestar_controller.catalog.coords import J2000Coord
from seestar_controller.link.frames import (
    DownloadedFrame,
    FrameMeta,
    atomic_write_json,
    meta_to_sidecar,
)
from seestar_controller.link.image import Frame
from seestar_controller.link.mock_scope import make_fits_bytes
from seestar_controller.link.preview import PreviewFrame, decode_payload
from seestar_controller.observing import (
    ExecutorState,
    FirstSubFrame,
    ObserveController,
    ObserveViewState,
    PlanQueueController,
    QueueEntry,
    SessionCapture,
)
from seestar_controller.observing.observe_controller import (
    FIRST_SUB_PREVIEW_MAX_PX,
    FIRST_SUB_SOURCE,
    load_first_sub_frame,
)
from seestar_controller.store.frames import list_frames, store_frame
from seestar_controller.store.project import create_project
from seestar_controller.store.sessions import start_session
from seestar_controller.ui.connection import ConnectionController, ConnectionStatus
from seestar_controller.ui.notifications import NotificationBus
from seestar_controller.ui.settings import SettingsController
from seestar_controller.ui.state import AppState


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def coord_entry(ra_hours: float, dec_deg: float, **kwargs) -> QueueEntry:
    return QueueEntry(coord=J2000Coord.from_hours(ra_hours, dec_deg), **kwargs)


class FakeDevice:
    """Duck-typed Device facade: the executor shape (goto/park/stack + event
    and stacking-progress handlers) plus a scripted ``download_frames`` that
    writes real FITS + sidecars (the test_observing_capture shape)."""

    def __init__(self, host: str = "10.9.8.7"):
        self.calls: list[tuple] = []
        self.transport = SimpleNamespace(host=host)
        self._handlers: dict[str, list] = {}
        self._progress_handlers: list = []
        self.frames: list[dict] = []
        self._seq = 0
        self._lock = threading.Lock()

    def goto(self, ra, dec, *, timeout=None):
        with self._lock:
            self.calls.append(("goto", ra, dec))
        return SimpleNamespace(mode="idle", target=(ra, dec))

    def park(self):
        with self._lock:
            self.calls.append(("park",))
        return SimpleNamespace(mode="parked")

    def start_stack(self, exposure, gain, mosaic=False, *, timeout=None):
        with self._lock:
            self.calls.append(("start_stack", exposure, gain, mosaic))
        return SimpleNamespace(mode="stacking")

    def stop_stack(self, *, timeout=None):
        with self._lock:
            self.calls.append(("stop_stack",))
        return SimpleNamespace(mode="idle")

    def state(self):
        return SimpleNamespace(mode="idle")

    def add_event_handler(self, name, handler):
        self._handlers.setdefault(name, []).append(handler)

    def remove_event_handler(self, name, handler):
        handlers = self._handlers.get(name, [])
        if handler in handlers:
            handlers.remove(handler)

    def add_stacking_progress_handler(self, handler):
        self._progress_handlers.append(handler)

    def remove_stacking_progress_handler(self, handler):
        if handler in self._progress_handlers:
            self._progress_handlers.remove(handler)

    def emit_progress(self, frames: int, state: str = "working", lapse_ms: int = 0):
        from seestar_controller.link.device import StackingProgress

        progress = StackingProgress(state=state, lapse_ms=lapse_ms, frames=frames)
        for handler in list(self._progress_handlers):
            handler(progress)

    def calls_of(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]

    def add_frame(self, kind, *, exposure=10.0, gain=80, target=None, when=None, size=(16, 16)):
        """Record one frame (real FITS bytes) the downloads can serve."""
        with self._lock:
            self._seq += 1
            seq = self._seq
            frame_id = f"{kind}-{seq:04d}"
            when = when or datetime.now(UTC)
            fits = make_fits_bytes(
                when=when, exposure=exposure, gain=gain, target=target, seed=seq, size=size
            )
            self.frames.append(
                {
                    "id": frame_id,
                    "kind": kind,
                    "time": when.isoformat(),
                    "epoch": when.timestamp(),
                    "exposure": float(exposure),
                    "gain": int(gain),
                    "target": dict(target) if target is not None else None,
                    "fits": fits,
                }
            )

    def download_frames(self, dest_dir, kind="sub", *, since=None, timeout=None):
        with self._lock:
            since_epoch = None if since is None else float(since)
            records = [
                r
                for r in self.frames
                if r["kind"] == kind and (since_epoch is None or r["epoch"] >= since_epoch)
            ]
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        downloaded: list[DownloadedFrame] = []
        for record in records:
            meta = FrameMeta(
                id=record["id"],
                kind=record["kind"],
                time=record["time"],
                epoch=record["epoch"],
                exposure=record["exposure"],
                gain=record["gain"],
                target=record["target"],
            )
            fits_path = dest / f"{record['id']}.fits"
            sidecar_path = dest / f"{record['id']}.json"
            fits_path.write_bytes(record["fits"])
            atomic_write_json(sidecar_path, meta_to_sidecar(meta, size=len(record["fits"])))
            downloaded.append(DownloadedFrame(path=fits_path, sidecar=sidecar_path, meta=meta))
        return downloaded


class FakePreviewStream:
    """Duck-typed preview stream that starts but NEVER emits a frame — the
    live stream stays idle, like the solar-filter-indoors case."""

    def __init__(self):
        self.callbacks: list = []
        self.running = False
        self.start_calls: list[tuple] = []

    def add_callback(self, callback):
        self.callbacks.append(callback)

    def start(self, tele: bool = True, wide: bool = False):
        self.start_calls.append((tele, wide))
        self.running = True

    def stop(self):
        self.running = False

    def emit(self, frame):
        for callback in list(self.callbacks):
            callback(frame)


def make_preview_frame(frame_id: int, width: int = 4, height: int = 3) -> PreviewFrame:
    size = width * height
    payload = bytes([frame_id % 256]) * size
    frame = Frame(code=0, id=frame_id, width=width, height=height, size=size, payload=payload)
    return PreviewFrame(
        source="tele", image=decode_payload(frame), frame=frame, timestamp=time.monotonic()
    )


def slow_capture_factory(device, project, session_id, executor, **kwargs):
    """A real SessionCapture parked on a long poll interval, so the test
    drives the store step deterministically via ``poll_once``."""
    kwargs["poll_interval_s"] = 60.0
    return SessionCapture(device, project, session_id, executor=executor, **kwargs)


def build_harness(tmp_path, *, invoke_on_main=None):
    """The app-level controllers with fakes — headless, no network, no gi
    (mirrors tests/test_observing_observe.build_harness, but with a
    FakeDevice that can download frames and a parked capture worker)."""
    state = AppState(tmp_path / "settings.json")
    state.set_setting("scope_ip", "10.9.8.7")
    settings = SettingsController(state)
    device = FakeDevice()
    connection = ConnectionController(settings, device_factory=lambda ip, key_path: device)
    bus = NotificationBus()
    queue_controller = PlanQueueController()
    streams: list[FakePreviewStream] = []

    def stream_factory(dev, host):
        stream = FakePreviewStream()
        streams.append(stream)
        return stream

    controller = ObserveController(
        connection,
        queue_controller,
        bus,
        preview_stream_factory=stream_factory,
        capture_factory=slow_capture_factory,
        invoke_on_main=invoke_on_main,
        preview_max_fps=None,
    )
    connection.connect()
    assert connection.wait(2.0)
    assert connection.status == ConnectionStatus.CONNECTED
    return SimpleNamespace(
        tmp_path=tmp_path,
        device=device,
        connection=connection,
        bus=bus,
        queue_controller=queue_controller,
        streams=streams,
        controller=controller,
    )


@pytest.fixture()
def harness(tmp_path):
    return build_harness(tmp_path)


@pytest.fixture()
def project(tmp_path):
    p = create_project("First Sub Night", root=tmp_path / "projects")
    yield p
    p.close()


# --- load_first_sub_frame: the decode-once, downscaled loader ----------------------


def test_load_first_sub_frame_returns_the_first_sub_downscaled(project, tmp_path):
    session = start_session(project)
    base = datetime(2026, 11, 22, 22, 0, 0, tzinfo=UTC)
    first = store_frame(
        project, session, "sub", make_fits_bytes(when=base, exposure=10.0, gain=80, seed=1,
                                                  size=(1200, 900))
    )
    store_frame(
        project, session, "sub", make_fits_bytes(when=base + timedelta(seconds=10),
                                                  exposure=10.0, gain=80, seed=2)
    )
    result = load_first_sub_frame(project, session)
    assert isinstance(result, FirstSubFrame)
    assert result.frame_id == first.id  # the FIRST sub (jd order), not the latest
    assert result.path == first.path
    assert result.source == FIRST_SUB_SOURCE
    assert result.id == first.id
    assert result.image.dtype == np.uint8
    assert result.image.ndim == 2  # grayscale FITS stays grayscale
    # downscaled to at most FIRST_SUB_PREVIEW_MAX_PX on the long side
    assert max(result.image.shape) <= FIRST_SUB_PREVIEW_MAX_PX
    assert (result.width, result.height) == (result.image.shape[1], result.image.shape[0])
    # the stretched image actually uses the dynamic range (not a flat gray)
    assert result.image.min() < result.image.max()


def test_load_first_sub_frame_none_until_a_sub_is_stored(project):
    session = start_session(project)
    assert load_first_sub_frame(project, session) is None
    # a Stack alone does not count — only sub-frames are the first exposure
    store_frame(
        project, session, "stack",
        make_fits_bytes(when=datetime.now(UTC), exposure=10.0, gain=80),
    )
    assert load_first_sub_frame(project, session) is None


def test_load_first_sub_frame_handles_rgb_fits(project):
    from astropy.io import fits as pyfits

    session = start_session(project)
    rng = np.random.default_rng(7)
    data = rng.integers(100, 900, size=(24, 32, 3)).astype(np.int16)
    hdu = pyfits.PrimaryHDU(data=data)
    hdu.header["DATE-OBS"] = datetime.now(UTC).isoformat()
    buf = __import__("io").BytesIO()
    pyfits.HDUList([hdu]).writeto(buf)
    frame = store_frame(project, session, "sub", buf.getvalue())
    result = load_first_sub_frame(project, session, max_px=16)
    assert result is not None and result.frame_id == frame.id
    assert result.image.ndim == 3 and result.image.shape[2] == 3  # RGB kept
    assert max(result.image.shape[:2]) <= 16


# --- the wiring: store first sub -> the observe view state updates, no live stream --


def test_first_sub_stored_during_night_updates_the_view_state_without_live_stream(
    harness, project
):
    qc = harness.queue_controller
    qc.add_entry(coord_entry(5.5, -5.0))
    states: list[ObserveViewState] = []
    harness.controller.add_listener(states.append)

    ex = harness.controller.start_night(
        project, frames_per_target=100, duration_per_target_s=30.0
    )
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    stream = harness.streams[0]
    assert stream.running and stream.start_calls == [(True, False)]
    capture = harness.controller.capture
    assert capture is not None and capture.running

    # no live frame yet, no stored sub yet -> no preview at all
    assert harness.controller.snapshot.preview_frame is None
    assert harness.controller.snapshot.first_sub_frame is None
    assert harness.controller.first_sub_frame is None

    # the first sub lands on the scope and is downloaded + stored
    harness.device.add_frame("sub", exposure=10.0, gain=80)
    stored = capture.poll_once()
    assert stored == 1

    # the frame-stored event wired the first-sub fallback (live stream still idle)
    snap = harness.controller.snapshot
    assert snap.preview_frame is None  # the live stream never emitted
    first_sub = snap.first_sub_frame
    assert isinstance(first_sub, FirstSubFrame)
    subs = list_frames(project, capture.session_id, kind="sub")
    assert len(subs) == 1
    assert first_sub.frame_id == subs[0].id
    assert first_sub.source == FIRST_SUB_SOURCE
    assert isinstance(first_sub.image, np.ndarray)
    assert first_sub.image.dtype == np.uint8
    assert max(first_sub.image.shape) <= FIRST_SUB_PREVIEW_MAX_PX
    # the view listeners were re-emitted with the fallback in the state
    assert any(s.first_sub_frame is first_sub for s in states)
    assert states[-1].first_sub_frame is first_sub
    assert states[-1].preview_frame is None

    # decoded ONCE: a second stored sub does not replace the first-sub preview
    harness.device.add_frame("sub", exposure=10.0, gain=80)
    assert capture.poll_once() == 1
    assert len(list_frames(project, capture.session_id, kind="sub")) == 2
    assert harness.controller.snapshot.first_sub_frame is first_sub
    assert harness.controller.snapshot.first_sub_frame.frame_id == subs[0].id

    # the live preview still takes over once it streams (fallback stays cached)
    stream.emit(make_preview_frame(1))
    snap = harness.controller.snapshot
    assert snap.preview_frame is not None and snap.preview_frame.id == 1
    assert snap.first_sub_frame is first_sub

    harness.controller.stop()
    assert ex.wait(timeout=5)


def test_first_sub_load_is_marshalled_via_invoke_on_main(tmp_path, project):
    """The frame-stored event does not decode inline: the load is scheduled
    through ``invoke_on_main`` and only runs when that is drained."""
    pending: list = []

    def queueing_invoke(callback):
        pending.append(callback)

    harness = build_harness(tmp_path, invoke_on_main=queueing_invoke)
    harness.queue_controller.add_entry(coord_entry(5.5, -5.0))
    harness.controller.start_night(project, frames_per_target=100, duration_per_target_s=30.0)
    capture = harness.controller.capture
    assert capture is not None

    harness.device.add_frame("sub", exposure=10.0, gain=80)
    assert capture.poll_once() == 1  # stores + logs the frame event
    # the load was SCHEDULED, not run: the state still has no fallback yet
    assert any(cb is not None for cb in pending)
    assert harness.controller.snapshot.first_sub_frame is None

    while pending:
        pending.pop(0)()  # drain the main loop
    first_sub = harness.controller.snapshot.first_sub_frame
    assert isinstance(first_sub, FirstSubFrame)
    assert first_sub.frame_id == list_frames(project, capture.session_id, kind="sub")[0].id

    harness.controller.stop()


def test_first_sub_preview_resets_on_the_next_night(harness, project):
    harness.queue_controller.add_entry(coord_entry(5.5, -5.0))
    ex = harness.controller.start_night(
        project, frames_per_target=100, duration_per_target_s=30.0
    )
    assert wait_until(lambda: ex.state == ExecutorState.STACKING)
    capture = harness.controller.capture
    harness.device.add_frame("sub", exposure=10.0, gain=80)
    capture.poll_once()
    assert harness.controller.snapshot.first_sub_frame is not None
    harness.controller.stop()
    assert ex.wait(timeout=5)
    assert ex.snapshot.state == ExecutorState.DONE
    # the capture was stopped and its listener removed
    assert not capture.running
    assert harness.controller._on_capture_log_event not in capture.listeners

    # a new night starts with no first-sub preview until its first sub lands
    ex2 = harness.controller.start_night(
        project, frames_per_target=100, duration_per_target_s=30.0
    )
    assert harness.controller.snapshot.first_sub_frame is None
    assert harness.controller.capture is not capture
    harness.controller.stop()
    assert ex2.wait(timeout=5)


# --- widget layer (skips headless): the Observe view renders the fallback -----------
#
# Same guards as tests/test_observing_widgets.py: ``pytest.importorskip("gi")``
# + the Gtk 4.0 / Adw 1 typelibs + a ``Gtk.init_check()`` (no display -> skip).
# The gi import happens INSIDE the test so the pure tests above still run in
# the headless sandbox (no gi module there).


class StubObserveController:
    """Minimal duck-typed ObserveController for the widget test."""

    def __init__(self, queue_controller):
        self._queue_controller = queue_controller
        self.connection = SimpleNamespace(status=ConnectionStatus.CONNECTED)
        self.project = object()
        self.capture = None
        self._snapshot = ObserveViewState(
            executor_state=ExecutorState.STACKING,
            queue_index=0,
            entry=None,
            frames_stacked=1,
            elapsed_s=10.0,
            error=None,
            session="sess-1",
            queue_entries=queue_controller.entries,
            preview_frame=None,
            preview_running=True,
            night_active=True,
        )
        self.listeners: list = []

    @property
    def queue_controller(self):
        return self._queue_controller

    @property
    def snapshot(self):
        return self._snapshot

    def set_snapshot(self, **changes):
        self._snapshot = dataclasses.replace(self._snapshot, **changes)
        for listener in list(self.listeners):
            listener(self._snapshot)

    def add_listener(self, listener):
        self.listeners.append(listener)
        return listener

    def remove_listener(self, listener):
        if listener in self.listeners:
            self.listeners.remove(listener)

    def start_night(self, project=None, **kwargs):
        return SimpleNamespace()

    def pause(self):
        pass

    def resume(self):
        pass

    def skip(self):
        pass

    def stop(self):
        pass

    def park(self):
        pass


def _first_sub_stub() -> FirstSubFrame:
    image = np.arange(6 * 8, dtype=np.uint8).reshape(6, 8) * 30
    return FirstSubFrame(frame_id=3, path="subs/sess-1/2461365.416668.fits", image=image)


def test_observe_view_shows_first_sub_fallback_then_live_takes_over():
    gi = pytest.importorskip("gi")
    try:
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
    except ValueError as exc:  # typelibs missing
        pytest.skip(f"GTK 4.0 / libadwaita typelibs missing: {exc}")
    from gi.repository import Gtk  # ty: ignore[missing-module-member]

    try:
        ok = Gtk.init_check()
    except Exception as exc:  # pragma: no cover - backend errors  # noqa: BLE001
        pytest.skip(f"Gtk.init_check failed: {exc}")
    if not ok:
        pytest.skip("no display available (headless environment)")

    from seestar_controller.observing.observe_view import ObserveView

    queue_controller = PlanQueueController()
    stub = StubObserveController(queue_controller)
    view = ObserveView(stub, queue_controller=queue_controller)  # ty: ignore[invalid-argument-type]

    # no live frame yet: the stored first sub is the preview fallback
    stub.set_snapshot(first_sub_frame=_first_sub_stub())
    assert view.picture.get_paintable() is not None
    assert not view.preview_placeholder.get_visible()
    label = view.preview_label.get_label()
    assert "first sub frame #3" in label
    assert "8×6" in label

    # the live preview stream takes over once it emits a frame
    stub.set_snapshot(preview_frame=make_preview_frame(9, width=8, height=6))
    assert view.picture.get_paintable() is not None
    label = view.preview_label.get_label()
    assert "tele frame #9" in label

    # and back to the fallback if the live frame is gone again (same object:
    # no re-render needed, the texture stays)
    stub.set_snapshot(preview_frame=None)
    assert view.picture.get_paintable() is not None
    assert "first sub frame #3" in view.preview_label.get_label()

    # with neither a live frame nor a stored sub: the placeholder is back
    stub.set_snapshot(first_sub_frame=None)
    assert view.preview_placeholder.get_visible()
