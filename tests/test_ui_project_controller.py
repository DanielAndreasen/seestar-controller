"""Pure-Python project controller tests (projects-view tickets 01 + 02) — no gi.

Every test runs against a ``tmp_path`` project root + settings file, so
nothing touches the real ``~/SeestarProjects`` or ``~/.config``. Ticket 02
adds the end-to-end create behavior: an unset ``project_root`` falls back to
the store default (``SEESTAR_PROJECT_ROOT`` > ``~/SeestarProjects``), a
missing root is created on demand at create time, an existing-but-unusable
root (not a directory / not writable) raises :class:`ProjectRootError` with
a Settings hint and changes nothing, and the created project is persisted
as ``last_project`` and restored (and pushed to the attached controllers)
after a "restart". The module-boundary tests run a subprocess / scan the
source asserting the pure layer never imports ``gi`` or ``link`` (mirrors
tests/test_ui_harness.py).
"""

import dataclasses
import re
import subprocess
import sys
from pathlib import Path

import pytest

from seestar_controller.store import (
    PROJECT_ROOT_ENV_VAR,
    Project,
    create_project,
    default_project_root,
    list_projects,
    open_project,
)
from seestar_controller.store.errors import (
    InvalidProjectError,
    ProjectExistsError,
    ProjectNotFoundError,
)
from seestar_controller.ui.project_controller import (
    ProjectController,
    ProjectEvent,
    ProjectRootError,
)
from seestar_controller.ui.state import (
    AppState,
    SettingsValidationError,
    load_state,
)


@pytest.fixture()
def settings_file(tmp_path):
    return tmp_path / "config" / "settings.json"


@pytest.fixture()
def root(tmp_path):
    """A tmp project root, wired into the state via the project_root setting."""
    project_root = tmp_path / "projects"
    project_root.mkdir()
    return project_root


@pytest.fixture()
def state(settings_file, root):
    state = AppState(settings_file)
    state.set_setting("project_root", str(root))
    return state


@pytest.fixture()
def controller(state):
    return ProjectController(state)


# --- root resolution ---------------------------------------------------------------


def test_root_uses_the_project_root_setting(state, root):
    assert ProjectController(state).root == root


def test_root_falls_back_to_the_env_var_when_setting_empty(settings_file, tmp_path, monkeypatch):
    env_root = tmp_path / "env-root"
    monkeypatch.setenv(PROJECT_ROOT_ENV_VAR, str(env_root))
    state = AppState(settings_file)
    state.set_setting("project_root", "")  # unset -> store default (env var)
    assert ProjectController(state).root == env_root


def test_root_explicit_argument_overrides_setting_and_env(state, root, tmp_path, monkeypatch):
    monkeypatch.setenv(PROJECT_ROOT_ENV_VAR, str(tmp_path / "env-root"))
    override = tmp_path / "override"
    assert ProjectController(state, root=override).root == override
    assert ProjectController(state, root=str(override)).root == override


def test_missing_root_lists_no_projects(controller):
    assert controller.projects == ()


# --- create / list / open ------------------------------------------------------------


def test_create_project_makes_it_current_and_lists_it(controller, root):
    project = controller.create_project("M 42")
    assert isinstance(project, Project)
    assert project.name == "M 42"
    assert project.path == root / "m-42"
    assert (project.path / "project.json").is_file()
    assert controller.current is project
    assert [s.name for s in controller.projects] == ["M 42"]
    assert controller.projects[0].slug == "m-42"
    assert controller.projects[0].path == project.path


def test_create_project_persists_last_project(controller, state, settings_file):
    project = controller.create_project("M 31")
    assert state.get_setting("last_project") == str(project.path)
    assert settings_file.is_file()  # _set_current saves
    fresh = load_state(settings_file)
    assert fresh.get_setting("last_project") == str(project.path)


def test_open_project_by_path_and_by_string(controller, root):
    first = controller.create_project("M 42")
    second = controller.create_project("M 31")
    assert controller.current is second
    opened = controller.open_project(first.path)
    assert opened.path == first.path
    assert controller.current is opened
    assert controller.state.get_setting("last_project") == str(first.path)
    opened2 = controller.open_project(str(second.path))
    assert controller.current is opened2


def test_listing_is_sorted_oldest_first(controller):
    controller.create_project("first")
    controller.create_project("second")
    controller.create_project("third")
    names = [s.name for s in controller.projects]
    assert names == ["first", "second", "third"]
    controller.refresh()
    assert [s.name for s in controller.projects] == names


def test_refresh_picks_up_externally_created_projects(controller, root):
    create_project("external", root=root)
    assert [s.name for s in controller.projects] == []  # not refreshed yet
    controller.refresh()
    assert [s.name for s in controller.projects] == ["external"]


# --- events ---------------------------------------------------------------------------


def test_events_are_fired_on_refresh_create_open(controller):
    events: list[ProjectEvent] = []
    controller.add_listener(events.append)
    controller.refresh()
    project = controller.create_project("M 42")
    controller.open_project(project.path)
    assert len(events) == 3
    assert all(isinstance(e, ProjectEvent) for e in events)
    assert events[-1].current is not None
    assert events[-1].current.path == project.path  # open_project re-opens it
    assert events[-1].root == str(controller.root)
    assert [s.name for s in events[-1].projects] == ["M 42"]


def test_event_is_frozen(controller):
    event = ProjectEvent(root="/r", projects=(), current=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.root = "/other"  # type: ignore[misc]  # ty: ignore[invalid-assignment]
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.current = None  # type: ignore[misc]  # ty: ignore[invalid-assignment]


def test_remove_listener_stops_events(controller):
    events: list[ProjectEvent] = []
    controller.add_listener(events.append)
    controller.remove_listener(events.append)
    controller.create_project("M 42")
    assert events == []
    assert controller.listeners == ()


def test_add_listener_is_idempotent(controller):
    events: list[ProjectEvent] = []
    controller.add_listener(events.append)
    controller.add_listener(events.append)
    assert controller.listeners == (events.append,)
    controller.refresh()
    assert len(events) == 1


# --- restore / last_project persistence -------------------------------------------------


def test_restore_opens_the_persisted_last_project(settings_file, root):
    state = AppState(settings_file)
    state.set_setting("project_root", str(root))
    project = create_project("M 42", root=root)
    state.set_setting("last_project", str(project.path))
    state.save()

    fresh = load_state(settings_file)
    controller = ProjectController(fresh)
    assert controller.current is None  # not restored until restore() is called
    restored = controller.restore()
    assert restored is not None
    assert restored.path == project.path
    assert controller.current is restored


def test_restore_without_last_project_returns_none(controller):
    assert controller.restore() is None
    assert controller.current is None


def test_restore_clears_a_stale_last_project(settings_file, root):
    state = AppState(settings_file)
    state.set_setting("project_root", str(root))
    project = create_project("M 42", root=root)
    state.set_setting("last_project", str(project.path))
    state.save()
    # the project directory disappears after the save
    import shutil

    shutil.rmtree(project.path)

    fresh = load_state(settings_file)
    # load() itself drops the stale value (validation), so last_project is ""
    assert fresh.get_setting("last_project") == ""
    controller = ProjectController(fresh)
    assert controller.restore() is None
    assert controller.current is None


def test_restore_clears_stale_value_when_load_kept_it(settings_file, root):
    """restore() also clears a stale last_project that survived load (e.g.
    the project became invalid between load and restore)."""
    state = AppState(settings_file)
    state.set_setting("project_root", str(root))
    project = create_project("M 42", root=root)
    state.set_setting("last_project", str(project.path))
    state.save()
    # corrupt the project.json after load kept the value valid at load time
    fresh = load_state(settings_file)
    assert fresh.get_setting("last_project") == str(project.path)
    (project.path / "project.json").write_text("{ not json", encoding="utf-8")
    controller = ProjectController(fresh)
    assert controller.restore() is None
    assert controller.current is None
    assert fresh.get_setting("last_project") == ""
    assert load_state(settings_file).get_setting("last_project") == ""


def test_last_project_setting_validation(state, root):
    assert state.get_setting("last_project") == ""
    state.set_setting("last_project", "")  # empty = unset
    project = create_project("M 42", root=root)
    state.set_setting("last_project", str(project.path))
    assert state.get_setting("last_project") == str(project.path)
    with pytest.raises(SettingsValidationError):
        state.set_setting("last_project", str(root / "missing"))
    with pytest.raises(SettingsValidationError):
        state.set_setting("last_project", str(root))  # dir without project.json
    with pytest.raises(SettingsValidationError):
        state.set_setting("last_project", 42)


# --- error cases: nothing changes, no event ---------------------------------------------


def test_create_duplicate_name_raises_and_changes_nothing(controller, state):
    controller.create_project("M 42")
    events: list[ProjectEvent] = []
    controller.add_listener(events.append)
    with pytest.raises(ProjectExistsError):
        controller.create_project("m 42")  # same slug
    assert controller.current is not None
    assert controller.current.name == "M 42"
    assert state.get_setting("last_project") == str(controller.current.path)
    assert events == []
    assert len(controller.projects) == 1


@pytest.mark.parametrize("bad_name", ["", "   ", "!!!", 42, None])
def test_create_invalid_name_raises_value_error(controller, bad_name):
    with pytest.raises(ValueError):
        controller.create_project(bad_name)
    assert controller.current is None
    assert controller.projects == ()


def test_open_missing_path_raises_and_changes_nothing(controller, root):
    controller.create_project("M 42")
    events: list[ProjectEvent] = []
    controller.add_listener(events.append)
    with pytest.raises(ProjectNotFoundError):
        controller.open_project(root / "nope")
    assert controller.current is not None
    assert controller.current.name == "M 42"
    assert events == []


def test_open_non_project_directory_raises(controller, root):
    stray = root / "stray"
    stray.mkdir()
    with pytest.raises(InvalidProjectError):
        controller.open_project(stray)
    assert controller.current is None


def test_open_corrupt_project_json_raises(controller, root):
    project = create_project("M 42", root=root)
    (project.path / "project.json").write_text("{ nope", encoding="utf-8")
    with pytest.raises(InvalidProjectError):
        controller.open_project(project.path)
    assert controller.current is None


# --- attach_controllers ------------------------------------------------------------------


class _Follower:
    def __init__(self):
        self.seen: list = []

    def set_project(self, project):
        self.seen.append(project)


def test_attach_controllers_pushes_current_on_change(controller):
    observe, viewer, picker = _Follower(), _Follower(), _Follower()
    controller.attach_controllers(observe=observe, viewer=viewer, picker=picker, run=None)
    assert observe.seen == [None]  # applied immediately (no current yet)
    project = controller.create_project("M 42")
    assert observe.seen == [None, project]
    assert viewer.seen == [None, project]
    assert picker.seen == [None, project]
    other = controller.create_project("M 31")
    assert observe.seen[-1] is other


def test_attach_controllers_applies_a_restored_project_immediately(settings_file, root):
    state = AppState(settings_file)
    state.set_setting("project_root", str(root))
    project = create_project("M 42", root=root)
    state.set_setting("last_project", str(project.path))
    state.save()
    fresh = load_state(settings_file)
    controller = ProjectController(fresh)
    controller.restore()
    follower = _Follower()
    controller.attach_controllers(viewer=follower)
    assert follower.seen == [controller.current]
    assert follower.seen[0].path == project.path


# --- pure-module boundary ------------------------------------------------------------------


def test_module_source_has_no_gi_or_link_import():
    import seestar_controller.ui.project_controller as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import gi|from gi)\b", source, re.MULTILINE)
    assert "seestar_controller.link" not in source


def test_importing_the_controller_in_a_subprocess_pulls_no_gi_or_link():
    code = (
        "import sys;"
        "import seestar_controller.ui.project_controller;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.link' not in sys.modules, 'link imported';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_store_helpers_still_work_against_the_controller_root(controller, root):
    """Sanity: the controller's root is what the store helpers see."""
    controller.create_project("M 42")
    assert [s.name for s in list_projects(root)] == ["M 42"]
    assert open_project(root / "m-42").name == "M 42"


# --- ticket 02: root fallback, on-demand creation, root errors ---------------------------


def test_root_falls_back_to_the_store_default_when_setting_and_env_unset(settings_file, monkeypatch):
    """Fresh settings (project_root unset, no env var) -> the store default
    ``~/SeestarProjects`` (projects-view ticket 02)."""
    monkeypatch.delenv(PROJECT_ROOT_ENV_VAR, raising=False)
    state = AppState(settings_file)
    assert state.get_setting("project_root") == ""  # unset on a fresh state
    controller = ProjectController(state)
    assert controller.root == default_project_root() == Path.home() / "SeestarProjects"


def test_missing_root_is_created_on_demand_on_create(settings_file, tmp_path, monkeypatch):
    """A missing root is NOT an error: create_project creates it (with the
    project) on demand (projects-view ticket 02)."""
    missing = tmp_path / "missing-root"
    monkeypatch.setenv(PROJECT_ROOT_ENV_VAR, str(missing))
    state = AppState(settings_file)
    state.set_setting("project_root", "")  # unset -> env var default
    controller = ProjectController(state)
    assert controller.root == missing
    assert not missing.exists()
    assert controller.root_error is None  # missing is not an error
    assert controller.projects == ()
    project = controller.create_project("M 42")
    assert missing.is_dir()  # the root was created on demand
    assert project.path == missing / "m-42"
    assert (project.path / "project.json").is_file()
    assert controller.current is project
    assert state.get_setting("last_project") == str(project.path)
    assert [s.name for s in controller.projects] == ["M 42"]


def test_root_error_is_none_for_a_missing_or_writable_root(settings_file, tmp_path, monkeypatch):
    monkeypatch.setenv(PROJECT_ROOT_ENV_VAR, str(tmp_path / "missing"))
    state = AppState(settings_file)
    assert ProjectController(state).root_error is None  # missing -> created on demand
    root = tmp_path / "projects"
    root.mkdir()
    state.set_setting("project_root", str(root))
    assert ProjectController(state).root_error is None  # writable -> fine


def test_unwritable_root_raises_project_root_error_with_settings_hint(settings_file, tmp_path):
    """An existing but unwritable root: create raises ProjectRootError (an
    OSError) whose message points to Settings, and nothing changes
    (projects-view ticket 02)."""
    root = tmp_path / "projects"
    root.mkdir()
    root.chmod(0o500)  # r-x: readable/listable, not writable (we are not root)
    try:
        state = AppState(settings_file)
        state.set_setting("project_root", str(root))
        controller = ProjectController(state)
        assert controller.root_error is not None
        assert "not writable" in controller.root_error
        assert "Settings" in controller.root_error
        events: list[ProjectEvent] = []
        controller.add_listener(events.append)
        with pytest.raises(ProjectRootError) as exc_info:
            controller.create_project("M 42")
        assert isinstance(exc_info.value, OSError)  # the widget layer's except OSError catches it
        assert "Settings" in str(exc_info.value)
        assert controller.current is None
        assert state.get_setting("last_project") == ""
        assert events == []  # no event fired
        assert controller.projects == ()
        assert not (root / "m-42").exists()  # nothing was created
    finally:
        root.chmod(0o700)  # let tmp_path cleanup succeed


def test_root_that_is_a_file_raises_project_root_error(settings_file, tmp_path):
    file_root = tmp_path / "not-a-dir"
    file_root.write_text("x", encoding="utf-8")
    state = AppState(settings_file)
    state.set_setting("project_root", str(file_root))
    controller = ProjectController(state)
    assert controller.root_error is not None
    assert "not a directory" in controller.root_error
    assert "Settings" in controller.root_error
    with pytest.raises(ProjectRootError):
        controller.create_project("M 42")
    assert controller.current is None
    assert state.get_setting("last_project") == ""


def test_create_persists_last_project_and_a_restart_restores_it_on_all_controllers(
    settings_file, tmp_path, monkeypatch
):
    """End-to-end persistence (projects-view ticket 02): create -> the
    settings file carries last_project -> a "restart" (fresh state loaded
    from the same file + fresh controller) restores the project and pushes
    it to every attached shared controller."""
    root = tmp_path / "projects"
    monkeypatch.setenv(PROJECT_ROOT_ENV_VAR, str(root))
    state = AppState(settings_file)
    controller = ProjectController(state)
    project = controller.create_project("M 42")

    fresh = load_state(settings_file)  # the "restart"
    assert fresh.get_setting("last_project") == str(project.path)
    restarted = ProjectController(fresh)
    assert restarted.current is None  # not restored until restore() is called
    followers = [_Follower() for _ in range(6)]  # observe/viewer/picker/run/lightcurve/period
    restarted.attach_controllers(
        observe=followers[0],
        viewer=followers[1],
        picker=followers[2],
        run=followers[3],
        lightcurve=followers[4],
        period=followers[5],
    )
    assert all(f.seen == [None] for f in followers)  # no current yet, applied immediately
    restored = restarted.restore()
    assert restored is not None
    assert restored.path == project.path
    assert restarted.current is restored
    assert all(f.seen[-1] is restored for f in followers)  # pushed to all six
    # and a later create on the restarted controller reaches them too
    other = restarted.create_project("M 31")
    assert all(f.seen[-1] is other for f in followers)
    assert load_state(settings_file).get_setting("last_project") == str(other.path)
