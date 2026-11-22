"""Widget-layer tests for sub-frames issue 03 — the **Stack subs** action
button on session rows in the Projects view's Stored-data browser (next to
"Stitch mosaic"): clicking it median-combines the session's subs into a new
``kind='stack'`` frame written back into the project, which then appears in
the Enhance viewer's stack list and opens there (the demo criterion of
``.scratch/sub-frames/issues/03-stack-subs.md``).

These SKIP in the headless sandbox (no ``gi`` module, no GTK system libs, no
display; see docs/wiki/assumptions.md) and run on the desktop (or under
``xvfb-run -a uv run pytest``) — the same guard pattern as
tests/test_offline_analysis_widgets.py. The button is clicked with
``.emit("clicked")`` (never ``.clicked()``) and no widget is ever
``destroy()``-ed (GTK4). No network.
"""

import time

import pytest
from test_stack_subs import T0, date_obs_str, fits_bytes, flat_data

gi = pytest.importorskip("gi")

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError as exc:  # typelibs missing
    pytest.skip(f"GTK 4.0 / libadwaita typelibs missing: {exc}", allow_module_level=True)

from gi.repository import GLib, Gtk

from seestar_controller.store import (
    KIND_STACK,
    KIND_SUB,
    close_session,
    create_project,
    list_frames,
    start_session,
    store_frame,
)
from seestar_controller.ui.connection import ConnectionStatus
from seestar_controller.ui.mode import AppMode

N_SUBS = 3


@pytest.fixture()
def gtk():
    """GTK initialized, or skip when no display is available (headless)."""
    try:
        ok = Gtk.init_check()
    except Exception as exc:  # pragma: no cover - backend errors  # noqa: BLE001
        pytest.skip(f"Gtk.init_check failed: {exc}")
    if not ok:
        pytest.skip("no display available (headless environment)")
    return Gtk


@pytest.fixture()
def app(gtk, tmp_path):
    """A SeestarApplication with its settings file in tmp_path (pattern:
    tests/test_offline_mode_widgets.py)."""
    from seestar_controller.ui.app import SeestarApplication

    application = SeestarApplication(settings_path=str(tmp_path / "settings.json"))
    application.state.set_setting("scope_ip", "192.168.1.50")
    return application


@pytest.fixture()
def project_root(app, tmp_path):
    """Point the app's project root at a tmp dir and refresh the list."""
    root = tmp_path / "projects"
    root.mkdir()
    app.state.set_setting("project_root", str(root))
    app.state.save()
    app.project_controller.refresh()
    return root


@pytest.fixture()
def stored_subs_only(project_root):
    """A synthetic project with ONLY subs (no stack): one closed session
    with N_SUBS flat sub-frames — the demo setup of the issue."""
    project = create_project("M 42 subs only", root=project_root)
    sid = start_session(project, started_at=T0)
    for i in range(N_SUBS):
        store_frame(
            project,
            sid,
            KIND_SUB,
            fits_bytes(flat_data(float(i + 1)), date_obs=date_obs_str(i)),
        )
    close_session(project, sid)
    return {"project": project, "sid": sid}


@pytest.fixture()
def stored_empty_session(project_root):
    """A synthetic project whose one session has NO frames at all (the
    error path: Stack subs has nothing to combine)."""
    project = create_project("empty night", root=project_root)
    sid = start_session(project, started_at=T0)
    close_session(project, sid)
    return {"project": project, "sid": sid}


def _pump() -> None:
    """Run all pending GLib main-context callbacks (the app's
    ``invoke_on_main`` is ``GLib.idle_add``)."""
    ctx = GLib.MainContext.default()
    while ctx.pending():
        ctx.iteration(False)


def _pump_until(predicate, timeout: float = 20.0) -> bool:
    """Pump the main context until ``predicate()`` is true."""
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while ctx.pending():
            ctx.iteration(False)
        if predicate():
            return True
        time.sleep(0.02)
    while ctx.pending():
        ctx.iteration(False)
    return predicate()


def _projects_page(window):
    from seestar_controller.ui.views import ProjectsView

    page = window.view_stack.get_child_by_name("projects")
    assert isinstance(page, ProjectsView)
    return page


def _open_offline(app, stored):
    """A window with the synthetic project opened offline."""
    from seestar_controller.ui.app import SeestarWindow

    app.project_controller.refresh()
    window = SeestarWindow(app, app.state)
    window.open_project_offline(stored["project"].path)
    _pump()
    return window


def _assert_link_untouched(window) -> None:
    assert window.connection_controller.status == ConnectionStatus.DISCONNECTED
    assert window.connection_controller.device is None


# --- the Stack subs button ---------------------------------------------------------------


def test_stack_subs_button_sits_next_to_stitch_and_report(app, gtk, stored_subs_only):
    window = _open_offline(app, stored_subs_only)
    page = _projects_page(window)
    assert page.session_count() == 1
    button = page.stack_subs_button(0)
    assert button.get_label() == "Stack subs"
    assert page.stitch_button(0).get_label() == "Stitch mosaic"
    assert page.report_button(0).get_label() == "Session report"
    _assert_link_untouched(window)


def test_stack_subs_button_stacks_subs_and_the_stack_lists_and_opens_in_the_viewer(
    app, gtk, stored_subs_only
):
    """The demo criterion: a project with only subs -> Stack subs -> a new
    stack frame in the project, listed by the Enhance viewer, and it opens."""
    from seestar_controller.enhance.fits_io import list_project_stacks

    window = _open_offline(app, stored_subs_only)
    assert window.mode == AppMode.OFFLINE
    page = _projects_page(window)
    project = app.project_controller.current
    sid = stored_subs_only["sid"]
    # before: subs only, no stack anywhere
    assert list_frames(project, session_id=sid, kind=KIND_SUB)
    assert list_frames(project, kind=KIND_STACK) == []
    assert list_project_stacks(project) == []

    page.click_stack_subs(0)
    assert _pump_until(lambda: page.data_status_label.get_label() != "")
    label = page.data_status_label.get_label()
    assert f"stacked {N_SUBS} subs" in label
    assert sid in label

    # the product: one new kind='stack' frame written back into the project
    stacks = list_frames(project, session_id=sid, kind=KIND_STACK)
    assert len(stacks) == 1
    assert f"stack #{stacks[0].id}" in label
    # it appears in the Enhance viewer's stack list ...
    infos = list_project_stacks(project)
    assert [info.frame_id for info in infos] == [stacks[0].id]
    # ... and opens there
    assert app.viewer_controller.project is project
    image = app.viewer_controller.open_stack(stacks[0].id)
    assert image.data.shape[:2] == flat_data(1.0).shape
    _assert_link_untouched(window)


def test_stack_subs_button_on_a_session_without_subs_reports_the_error_inline(
    app, gtk, stored_empty_session
):
    """Never raises: the failure is surfaced in the data status label."""
    window = _open_offline(app, stored_empty_session)
    page = _projects_page(window)
    project = app.project_controller.current
    sid = stored_empty_session["sid"]

    page.click_stack_subs(0)
    assert _pump_until(lambda: page.data_status_label.get_label() != "")
    label = page.data_status_label.get_label()
    assert "failed" in label
    assert sid in label
    # nothing was written
    assert list_frames(project, session_id=sid, kind=KIND_STACK) == []
    _assert_link_untouched(window)
