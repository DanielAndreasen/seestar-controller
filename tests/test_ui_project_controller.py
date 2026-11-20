"""Pure-Python project controller tests (projects-view ticket 01) — no gi.

Every test runs against a ``tmp_path`` project root + settings file, so
nothing touches the real ``~/SeestarProjects`` or ``~/.config``. The
module-boundary tests run a subprocess / scan the source asserting the pure
layer never imports ``gi`` or ``link`` (mirrors tests/test_ui_harness.py).
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
