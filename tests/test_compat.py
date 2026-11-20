"""fw 9.75 wire-compat shim: 103 fallback, app-state parsing, album frames flow.

The mock Scope emulates both wire dialects via ``firmware=`` ("legacy" is
the conftest default: fw 9.75 names answer 103; "9.75": legacy names answer
103; "both": all served). These tests pin the shim in :mod:`.link.compat`
and the fw 9.75 handlers in the mock, using the live-probed fw 9.75 facts
(Seestar S50, station mode, 2026-11-20 — see docs/wiki/progress.md).
"""

import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from seestar_controller.link import compat, frames
from seestar_controller.link.device import Device, _parse_app_state
from seestar_controller.link.mock_scope import MockScope, make_fits_bytes
from seestar_controller.link.transport import LinkTransport, RpcError


@pytest.fixture()
def mock975(keypair):
    """A running mock Scope speaking only the fw 9.75 dialect."""
    _private_pem, public_pem = keypair
    scope = MockScope(
        "127.0.0.1",
        0,
        0,
        0,
        public_pem=public_pem,
        frame_interval=0.01,
        firmware="9.75",
        slew_duration=0.05,
    )
    scope.start()
    yield scope
    scope.stop()


@pytest.fixture()
def transport975(mock975, keypair):
    """A LinkTransport connected to the fw 9.75 mock Scope."""
    private_pem, _public_pem = keypair
    t = LinkTransport(
        "127.0.0.1",
        mock975.command_port,
        pem=private_pem,
        heartbeat_interval=0.1,
        heartbeat_timeout=0.5,
        request_timeout=2.0,
    )
    t.connect()
    yield t
    t.disconnect()


@pytest.fixture()
def device975(transport975):
    d = Device.connect("127.0.0.1", transport=transport975, goto_timeout=2.0)
    yield d
    d.disconnect()


@pytest.fixture()
def mock_both(keypair):
    _private_pem, public_pem = keypair
    scope = MockScope(
        "127.0.0.1", 0, 0, 0, public_pem=public_pem, frame_interval=0.01, firmware="both"
    )
    scope.start()
    yield scope
    scope.stop()


# -- dialect gating: 103 on the other firmware -----------------------------------


def test_legacy_names_answer_103_on_975_mock(transport975):
    for method in (
        "get_scope_state",
        "scope_goto_target",
        "iscope_start_stacking",
        "iscope_stop_stacking",
        "list_frames",
        "get_frame",
    ):
        with pytest.raises(RpcError) as excinfo:
            transport975.request(method, {"kind": "sub"} if method == "list_frames" else None)
        assert excinfo.value.reply.code == compat.METHOD_NOT_FOUND_CODE == 103


def test_975_names_answer_103_on_legacy_mock(transport):
    for method in (
        compat.GET_APP_STATE_METHOD,
        compat.GOTO_METHOD_975,
        compat.START_STACK_METHOD_975,
        compat.STOP_STACK_METHOD_975,
        compat.GET_ALBUMS_METHOD,
        compat.PAGE_NUMBER_METHOD,
        compat.PAGE_NAME_METHOD,
    ):
        with pytest.raises(RpcError) as excinfo:
            transport.request(method)
        assert excinfo.value.reply.code == 103


def test_both_dialect_serves_all_names(mock_both, keypair):
    private_pem, _ = keypair
    t = LinkTransport(
        "127.0.0.1", mock_both.command_port, pem=private_pem, request_timeout=2.0
    )
    t.connect()
    try:
        assert t.request("get_scope_state")["mode"] == "idle"
        assert isinstance(t.request(compat.GET_APP_STATE_METHOD), dict)
        assert t.request(compat.GET_ALBUMS_METHOD)["path"] == frames.ROOT_DIR
        assert t.request("list_frames", {"kind": "sub"}) == {"frames": []}
    finally:
        t.disconnect()


def test_invalid_firmware_knob_rejected(keypair):
    _private_pem, public_pem = keypair
    with pytest.raises(ValueError):
        MockScope("127.0.0.1", 0, 0, 0, public_pem=public_pem, firmware="8.0")


# -- compat.call: fallback, caching, error propagation -----------------------------


def test_call_falls_back_on_103_and_caches(transport, mock_scope):
    """On the legacy mock the 9.75 name 103s, the legacy name wins, and the
    winner is cached so the next call does not re-probe."""
    compat.reset_cache()
    method, result = compat.call(transport, compat.STATE_METHODS)
    assert method == "get_scope_state"
    assert result["mode"] == "idle"
    assert compat.cached_method(transport, compat.STATE_METHODS) == "get_scope_state"

    calls: list[str] = []
    original = transport.request_raw

    def counting(method, params=None, *, timeout=None, suppress_scope_error=False):
        calls.append(method)
        return original(
            method, params, timeout=timeout, suppress_scope_error=suppress_scope_error
        )

    transport.request_raw = counting  # type: ignore[method-assign]
    try:
        compat.call(transport, compat.STATE_METHODS)
    finally:
        transport.request_raw = original  # type: ignore[method-assign]
    assert calls == ["get_scope_state"]  # exactly one call, no 103 re-probe


def test_call_propagates_non_103_errors_immediately(transport975):
    """A 207/108 answer is a real answer, not a version difference: raise."""
    with pytest.raises(RpcError) as excinfo:
        compat.call(
            transport975,
            compat.GOTO_METHODS,
            lambda _m: [99.0, 99.0],  # fw 9.75 scope_goto takes positional [ra, dec]
        )
    assert excinfo.value.reply.code == 207
    assert compat.cached_method(transport975, compat.GOTO_METHODS) is None


def test_call_all_103_raises_and_clears_cache(transport975):
    """When every candidate answers 103, the last reply is raised and no
    winner is cached."""
    with pytest.raises(RpcError) as excinfo:
        compat.call(transport975, ("get_scope_state", "scope_goto_target"))
    assert excinfo.value.reply.code == 103
    assert compat.cached_method(transport975, ("get_scope_state", "scope_goto_target")) is None


def test_reset_cache_forgets_winner(transport975, device975):
    device975.state()
    assert compat.cached_method(transport975, compat.STATE_METHODS) == compat.GET_APP_STATE_METHOD
    compat.reset_cache(transport975)
    assert compat.cached_method(transport975, compat.STATE_METHODS) is None
    compat.reset_cache()


# -- Device facade on the fw 9.75 dialect -------------------------------------------


def test_state_uses_app_state_on_975(device975, mock975):
    state = device975.state()
    assert state.mode == "idle"
    assert state.target is None
    assert state.stacking.state == "idle"
    assert compat.cached_method(device975.transport, compat.STATE_METHODS) == (
        compat.GET_APP_STATE_METHOD
    )


def test_state_reflects_slew_and_target_on_975(device975, mock975):
    mock975.slew_duration = 0.4
    import threading

    holder: dict = {}
    thread = threading.Thread(
        target=lambda: holder.setdefault("result", device975.goto(3.25, -15.5))
    )
    thread.start()
    try:
        saw_slewing = False
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = device975.state()
            if state.slewing:
                saw_slewing = True
                assert state.target == (3.25, -15.5)
                break
            time.sleep(0.01)
        assert saw_slewing, "state polling never saw mode=slewing"
    finally:
        thread.join(timeout=5)
    assert holder["result"].mode == "idle"
    assert holder["result"].target == (3.25, -15.5)
    assert mock975.last_goto == {"ra": 3.25, "dec": -15.5}
    assert compat.cached_method(device975.transport, compat.GOTO_METHODS) == (
        compat.GOTO_METHOD_975
    )


def test_goto_wire_params_on_975(device975, mock975):
    device975.goto(5.5, 12.25)
    # fw 9.75 scope_goto got POSITIONAL [ra_hours, dec_deg] on the wire
    # (seestarpy raw.scope_goto: 'params': [ra, dec]) — a dict would be
    # silently ignored by the firmware (code 0, no slew)
    assert mock975.last_wire_params[compat.GOTO_METHOD_975] == [5.5, 12.25]
    assert mock975.last_goto == {"ra": 5.5, "dec": 12.25}


def test_goto_bad_params_on_975_wire(transport975, mock975):
    # wrong types / wrong arity in the positional list -> 108, no motion
    for bad in ([None, None], ["x", 0.0], [1.0], [1.0, 2.0, 3.0]):
        with pytest.raises(RpcError) as excinfo:
            transport975.request(compat.GOTO_METHOD_975, bad)
        assert excinfo.value.reply.code == 108
    # out of range -> 207 "fail to operate", no motion
    for bad in ([99.0, 0.0], [0.0, -91.0]):
        with pytest.raises(RpcError) as excinfo:
            transport975.request(compat.GOTO_METHOD_975, bad)
        assert excinfo.value.reply.code == 207
    assert mock975.last_goto is None
    # dict params are silently ignored by fw 9.75: code 0 but NO slew
    assert transport975.request(compat.GOTO_METHOD_975, {"ra": 7.0, "dec": 8.0}) == 0
    assert mock975.last_goto is None
    # positional [ra, dec] slews
    assert transport975.request(compat.GOTO_METHOD_975, [7.0, 8.0]) == 0
    assert mock975.last_goto == {"ra": 7.0, "dec": 8.0}


def test_start_stop_stack_on_975(device975, mock975):
    mock975.stack_event_interval = 0.05
    state = device975.start_stack(10.0, 80)
    assert state.mode == "stacking"
    assert state.stacking.state == "working"
    # legacy stacking params are carried along, plus restart=true on 9.75
    assert mock975.last_stack_params == {"exp_ms": 10000, "gain": 80, "is_mosaic": False}
    assert compat.cached_method(device975.transport, compat.START_STACK_METHODS) == (
        compat.START_STACK_METHOD_975
    )
    time.sleep(0.2)
    progress = device975.state().stacking
    assert progress.frames >= 1
    assert progress.lapse_ms > 0

    state = device975.stop_stack()
    assert state.mode == "idle"
    assert compat.cached_method(device975.transport, compat.STOP_STACK_METHODS) == (
        compat.STOP_STACK_METHOD_975
    )


def test_start_stack_restart_while_running_is_259(transport975, mock975):
    mock975.start_stacking(10.0, 80)
    try:
        with pytest.raises(RpcError) as excinfo:
            transport975.request(
                compat.START_STACK_METHOD_975,
                {"restart": True, "exp_ms": 10000, "gain": 80, "is_mosaic": False},
            )
        assert excinfo.value.reply.code == compat.ALREADY_RUNNING_CODE == 259
        # restart=false while running is accepted (resume)
        assert (
            transport975.request(
                compat.START_STACK_METHOD_975,
                {"restart": False, "exp_ms": 10000, "gain": 80, "is_mosaic": False},
            )
            == 0
        )
    finally:
        mock975.stop_stacking()


def test_stop_view_stage_stack_noop_when_idle(transport975):
    # stopping just the stack while idle is a no-op with code 0 on fw 9.75
    assert transport975.request(compat.STOP_STACK_METHOD_975, {"stage": "Stack"}) == 0
    assert transport975.request(compat.STOP_STACK_METHOD_975) == 0


def test_park_still_works_on_975(device975, mock975):
    result = device975.park()
    assert result.parked
    assert device975.state().mode == "parked"
    assert mock975.scope_state()["mode"] == "parked"


# -- _parse_app_state unit shapes ----------------------------------------------------


def test_parse_app_state_idle_without_view():
    state = _parse_app_state({"FocuserMove": {"state": "complete"}, "ViewPlan": {}})
    assert state.mode == "idle"
    assert state.target is None
    assert state.stacking.state == "idle"
    assert state.stacking.frames == 0


def test_parse_app_state_slewing():
    state = _parse_app_state(
        {
            "View": {
                "state": "working",
                "target_ra_dec": [1.5, -20.0],
                "target_name": "M 31",
                "AutoGoto": {"state": "working"},
                "Stack": {"state": "idle", "lapse_ms": 0, "stacked_frame": 0},
            }
        }
    )
    assert state.mode == "slewing"
    assert state.target == (1.5, -20.0)


def test_parse_app_state_stacking():
    state = _parse_app_state(
        {
            "View": {
                "state": "working",
                "target_ra_dec": [1.5, -20.0],
                "Stack": {
                    "state": "working",
                    "lapse_ms": 42000,
                    "stacked_frame": 4,
                    "dropped_frame": 1,
                },
            }
        }
    )
    assert state.mode == "stacking"
    assert state.stacking == device_stacking("working", 42000, 4)


def test_parse_app_state_stack_node_at_root():
    state = _parse_app_state(
        {
            "Stack": {"state": "working", "lapse_ms": 1000, "stacked_frame": 1},
            "View": {"state": "working", "target_ra_dec": [0.0, 0.0]},
        }
    )
    assert state.mode == "stacking"
    assert state.stacking.frames == 1


def test_parse_app_state_park_stage():
    state = _parse_app_state({"View": {"state": "idle", "stage": "Park"}})
    assert state.mode == "parked"
    assert state.parked


def test_parse_app_state_stage_stack_means_stacking():
    # live fw 9.75 shape: View.stage is the authoritative activity field
    state = _parse_app_state(
        {
            "View": {
                "state": "working",
                "stage": "Stack",
                "target_ra_dec": [23.9556, 56.7261],
                "Stack": {
                    "state": "working",
                    "lapse_ms": 1000,
                    "stacked_frame": 1,
                    "dropped_frame": 0,
                },
            }
        }
    )
    assert state.mode == "stacking"
    assert state.stacking.frames == 1
    # the stage alone (no Stack child node) still means stacking
    assert _parse_app_state({"View": {"state": "working", "stage": "Stack"}}).mode == (
        "stacking"
    )


def test_parse_app_state_stage_autogoto_means_slewing():
    state = _parse_app_state(
        {"View": {"state": "working", "stage": "AutoGoto", "AutoGoto": {"state": "working"}}}
    )
    assert state.mode == "slewing"
    assert _parse_app_state({"View": {"state": "working", "stage": "ScopeGoto"}}).mode == (
        "slewing"
    )
    # root-level AutoGoto node (some builds hang it off the app-state root)
    state = _parse_app_state(
        {"AutoGoto": {"state": "working"}, "View": {"state": "working"}}
    )
    assert state.mode == "slewing"


def test_parse_app_state_continuous_exposure_is_idle():
    # live-probed fw 9.75 live view: stage ContinuousExposure, state working
    state = _parse_app_state(
        {
            "View": {
                "state": "working",
                "mode": "star",
                "stage": "ContinuousExposure",
                "target_name": "M 31",
                "target_ra_dec": [0.736843, 41.415901],
                "ContinuousExposure": {"state": "working", "fps": 2.0},
            }
        }
    )
    assert state.mode == "idle"
    assert state.target == (0.736843, 41.415901)
    assert state.target_name == "M 31"


def test_parse_app_state_target_name_and_stale_goto_hint():
    tree = {
        "View": {
            "state": "working",
            "stage": "Stack",
            "target_name": "M 31",  # stale: a coordinate goto does not update it
            "target_ra_dec": [23.9556, 56.7261],
            "Stack": {"state": "working", "lapse_ms": 0, "stacked_frame": 0},
        }
    }
    # without a hint the (stale) View.target_name is reported as-is
    assert _parse_app_state(tree).target_name == "M 31"
    # with a matching last-goto hint, the hint's name wins
    state = _parse_app_state(tree, goto_hint=(23.9556, 56.7261, "NGC 7789"))
    assert state.target_name == "NGC 7789"
    # a hint for different coordinates does not apply
    assert _parse_app_state(tree, goto_hint=(1.0, 2.0, "M 42")).target_name == "M 31"
    # a hint without a name changes nothing
    assert _parse_app_state(tree, goto_hint=(23.9556, 56.7261, None)).target_name == "M 31"
    # no target_name in the View at all -> the hint fills it in
    tree2 = {"View": {"state": "working", "stage": "Stack", "target_ra_dec": [23.9556, 56.7261]}}
    assert _parse_app_state(tree2).target_name is None
    assert (
        _parse_app_state(tree2, goto_hint=(23.9556, 56.7261, "NGC 7789")).target_name
        == "NGC 7789"
    )


def test_state_target_name_falls_back_to_last_goto_on_975(device975, mock975):
    """fw 9.75: a coordinate ``scope_goto`` does NOT update
    ``View.target_name`` (live-observed: after goto NGC 7789 the scope kept
    writing ``M 31_sub/Light_M 31_...`` sub-frames), so :meth:`Device.state`
    reports the last goto's name instead of the stale one."""
    mock975.set_view_target_name("M 31")  # stale name from a previous target
    device975.goto(23.9556, 56.7261, name="NGC 7789")
    state = device975.state()
    assert state.target == (23.9556, 56.7261)
    assert state.target_name == "NGC 7789"
    # the wire View still carries the stale name
    view = device975.transport.request(compat.GET_APP_STATE_METHOD)["View"]
    assert view["target_name"] == "M 31"
    assert view["target_ra_dec"] == [23.9556, 56.7261]


def test_state_reports_stacking_while_stacking_on_975(device975, mock975):
    """While the Stack is working the facade reports mode "stacking" (the
    fw 9.75 ``View.stage == "Stack"`` / ``View.Stack.state == "working"``)."""
    mock975.stack_event_interval = 0.05
    device975.goto(23.9556, 56.7261, name="NGC 7789")
    state = device975.start_stack(10.0, 80)
    assert state.mode == "stacking"
    assert state.stacking.state == "working"
    polled = device975.state()
    assert polled.mode == "stacking"
    assert polled.target_name == "NGC 7789"
    assert device975.stop_stack().mode == "idle"


def device_stacking(state, lapse_ms, frames):
    from seestar_controller.link.device import StackingProgress

    return StackingProgress(state=state, lapse_ms=lapse_ms, frames=frames)


# -- fw 9.75 frames flow: get_albums + paging + HTTP fetch ---------------------------


def test_http_frame_url_quotes_path():
    url = frames.http_frame_url(
        "192.168.87.185", "MyWorks/NGC 6888_sub/Light_NGC 6888_10.0s_LP_20250824-001334.fit"
    )
    assert url == (
        "http://192.168.87.185/MyWorks/NGC%206888_sub/"
        "Light_NGC%206888_10.0s_LP_20250824-001334.fit"
    )


def test_albums_listing_shape(transport975, mock975):
    mock975.add_frame("sub", target={"ra": 1.0, "dec": 2.0, "name": "NGC 6888"})
    mock975.add_frame("stack", target={"ra": 1.0, "dec": 2.0, "name": "NGC 6888"})
    albums = transport975.request(compat.GET_ALBUMS_METHOD)
    assert albums["path"] == "MyWorks"
    files = albums["list"][0]["files"]
    names = sorted(f["name"] for f in files)
    assert names == ["NGC 6888", "NGC 6888_sub"]
    sub = next(f for f in files if f["name"] == "NGC 6888_sub")
    assert sub["count"] == 1
    assert sub["thn"].startswith("NGC 6888_sub/")


def test_page_number_and_page_name(transport975, mock975):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(25):
        mock975.add_frame("sub", target={"name": "M 31"}, when=base + timedelta(seconds=i))
    # page count: 25 files / 20 per page = 2 pages; key is "dir", not "path"
    pages = transport975.request(
        compat.PAGE_NUMBER_METHOD, {"dir": "MyWorks/M 31_sub", "skip_avi": False}
    )
    assert pages == 2
    page0 = transport975.request(compat.PAGE_NAME_METHOD, {"page": 0})
    page1 = transport975.request(compat.PAGE_NAME_METHOD, {"page": 1})
    assert len(page0) == 20 and len(page1) == 5
    entry = page0[0]
    assert entry["is_dir"] is False
    assert entry["name"].startswith("Light_M 31_")
    assert entry["name"].endswith(".fit")
    assert entry["header_date"].startswith("2026-01-01T00:00:")
    assert entry["size_k"] > 0
    # out of range -> 204; wrong param type -> 104
    with pytest.raises(RpcError) as excinfo:
        transport975.request(compat.PAGE_NAME_METHOD, {"page": 2})
    assert excinfo.value.reply.code == 204
    with pytest.raises(RpcError) as excinfo:
        transport975.request(compat.PAGE_NAME_METHOD, {"page": [0]})
    assert excinfo.value.reply.code == 104
    # unknown dir -> 0 pages, and paging it -> 204
    assert (
        transport975.request(
            compat.PAGE_NUMBER_METHOD, {"dir": "MyWorks/Nope_sub", "skip_avi": False}
        )
        == 0
    )
    with pytest.raises(RpcError) as excinfo:
        transport975.request(compat.PAGE_NAME_METHOD, {"page": 0})
    assert excinfo.value.reply.code == 204
    # {"path": ...} is accepted but ignored (live-probed): still 0 pages
    assert (
        transport975.request(compat.PAGE_NUMBER_METHOD, {"path": "MyWorks/M 31_sub"}) == 0
    )


def test_page_name_without_cursor_lists_root(transport975, mock975):
    mock975.add_frame("sub", target={"name": "M 31"})
    entries = transport975.request(compat.PAGE_NAME_METHOD, {"page": 0})
    assert entries == [
        {
            "name": "MyWorks",
            "is_dir": True,
            "date": "1970-01-01 00:00:00",
            "file_cnt": 1,
            "size_k": entries[0]["size_k"],
        }
    ]


def test_list_remote_frames_albums_flow(transport975, mock975):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    mock975.add_frame("sub", target={"ra": 5.5, "dec": 12.25, "name": "NGC 6888"}, when=base)
    mock975.add_frame("sub", target={"ra": 5.5, "dec": 12.25, "name": "NGC 6888"}, when=base + timedelta(minutes=10))
    mock975.add_frame("stack", target={"ra": 5.5, "dec": 12.25, "name": "NGC 6888"}, when=base + timedelta(minutes=20))

    method, records = frames.list_remote_frames(transport975, "sub")
    assert method == compat.GET_ALBUMS_METHOD
    assert [r["id"] for r in records] == [
        "NGC 6888_sub/Light_NGC 6888_10s_20260101-000000.fit",
        "NGC 6888_sub/Light_NGC 6888_10s_20260101-001000.fit",
    ]
    assert all(r["remote_path"].startswith("MyWorks/NGC 6888_sub/") for r in records)
    assert records[0]["epoch"] < records[1]["epoch"]

    # kind=stack picks the plain (non-_sub) folder
    _method, stacks = frames.list_remote_frames(transport975, "stack")
    assert len(stacks) == 1
    assert stacks[0]["remote_path"].startswith("MyWorks/NGC 6888/")

    # since filters by listing epoch
    _method, recent = frames.list_remote_frames(
        transport975, "sub", since_epoch=(base + timedelta(minutes=5)).timestamp()
    )
    assert len(recent) == 1
    assert recent[0]["id"].endswith("001000.fit")


def test_download_frames_over_http_on_975(device975, mock975, tmp_path):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    mock975.add_frame(
        "sub", exposure=10.0, gain=80, target={"ra": 5.5, "dec": 12.25, "name": "NGC 6888"}, when=base
    )
    mock975.add_frame(
        "stack", exposure=120.0, gain=60, target={"ra": 5.5, "dec": 12.25, "name": "NGC 6888"}, when=base
    )

    subs = device975.download_frames(tmp_path, kind="sub", fetch=mock975.fetch_http)
    assert len(subs) == 1
    sub = subs[0]
    # id is the album path; the local file name is sanitized, extension stripped
    assert sub.meta.id == "NGC 6888_sub/Light_NGC 6888_10s_20260101-000000.fit"
    assert sub.path.name == "NGC_6888_sub_Light_NGC_6888_10s_20260101-000000.fits"
    assert sub.sidecar.name == sub.path.name.replace(".fits", ".json")
    sidecar = json.loads(sub.sidecar.read_text())
    # metadata merged from the FITS header (DATE-OBS/EXPTIME/GAIN/OBJECT/RA/DEC)
    assert sidecar["time"] == "2026-01-01T00:00:00.000"
    assert sidecar["exposure"] == 10.0
    assert sidecar["gain"] == 80
    assert sidecar["target"] == {"name": "NGC 6888", "ra": 5.5, "dec": 12.25}
    assert sidecar["size"] == sub.path.stat().st_size
    # the FITS file itself is valid and matches the sidecar
    from astropy.io import fits as pyfits

    with pyfits.open(sub.path) as hdul:
        assert str(hdul[0].header["DATE-OBS"]) == "2026-01-01T00:00:00.000"
        assert float(hdul[0].header["EXPTIME"]) == 10.0

    stacks = device975.download_frames(tmp_path, kind="stack", fetch=mock975.fetch_http)
    assert len(stacks) == 1
    assert stacks[0].meta.kind == "stack"
    assert stacks[0].path.name.startswith("NGC_6888_Stacked_")
    assert compat.cached_method(device975.transport, compat.FRAMES_LIST_METHODS) == (
        compat.GET_ALBUMS_METHOD
    )


def test_download_frames_http_404_raises(device975, mock975, tmp_path):
    mock975.add_frame("sub", target={"name": "M 31"})

    def broken_fetch(url):
        raise FileNotFoundError(url)

    with pytest.raises(FileNotFoundError):
        device975.download_frames(tmp_path, kind="sub", fetch=broken_fetch)
    assert list(tmp_path.glob("*.fits")) == []  # nothing partial left behind


def test_fetch_frame_bytes_legacy_uses_get_frame(transport, mock_scope):
    mock_scope.add_frame("sub", exposure=10.0, gain=80, target={"ra": 1.0, "dec": 2.0})
    _method, records = frames.list_remote_frames(transport, "sub")
    assert _method == frames.LIST_FRAMES_METHOD
    blob = frames.fetch_frame_bytes(transport, records[0])
    assert blob.startswith(b"SIMPLE")


def test_mock_fetch_http_serves_remote_path_and_404(mock975):
    record = mock975.add_frame("sub", target={"name": "M 31"})
    url = frames.http_frame_url("127.0.0.1", record.get("remote_path", ""))
    # add_frame returns the public record (no remote_path); rebuild from the store
    with mock975._frames_lock:
        remote = mock975._frames[-1]["remote_path"]
    url = frames.http_frame_url("127.0.0.1", remote)
    assert mock975.fetch_http(url).startswith(b"SIMPLE")
    with pytest.raises(FileNotFoundError):
        mock975.fetch_http("http://127.0.0.1/MyWorks/DeepSky/nope.fit")


# -- metadata helpers (unit) ----------------------------------------------------------


def test_parse_filename_meta():
    meta = frames.parse_filename_meta("Light_NGC 6888_10.0s_LP_20250824-001334.fit")
    assert meta["exposure"] == 10.0
    assert meta["target"] == {"name": "NGC 6888"}
    assert meta["time"] == "2025-08-24T00:13:34.000"
    assert meta["epoch"] == datetime(2025, 8, 24, 0, 13, 34, tzinfo=UTC).timestamp()
    stack = frames.parse_filename_meta("Stacked_3_M 31_240.0s_20250101-010203.fit")
    assert stack["exposure"] == 240.0
    assert stack["target"] == {"name": "M 31"}
    assert frames.parse_filename_meta("random_name.txt") == {}
    assert frames.parse_filename_meta("Light_x_10s_20250824-001334.fits")["exposure"] == 10.0


def test_read_fits_header_meta():
    when = datetime(2026, 5, 19, 22, 30, 15, 123000, tzinfo=UTC)
    blob = make_fits_bytes(
        when=when, exposure=10.0, gain=80, target={"ra": 5.5, "dec": 12.25, "name": "V* test"}
    )
    meta = frames.read_fits_header_meta(blob)
    assert meta["time"] == "2026-05-19T22:30:15.123"
    assert meta["exposure"] == 10.0
    assert meta["gain"] == 80
    assert meta["target"] == {"name": "V* test", "ra": 5.5, "dec": 12.25}
    assert frames.read_fits_header_meta(b"not a fits file") == {}


def test_local_stem_strips_fits_extension():
    assert frames._local_stem("a/b/Light_x_10s_20250824-001334.fit") == (
        "a/b/Light_x_10s_20250824-001334"
    )
    assert frames._local_stem("sub-0001") == "sub-0001"
    assert frames._local_stem("x.FITS") == "x"
