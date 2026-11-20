"""Pure-Python ui layer: app state, navigation, settings — no gi, tmp_path only.

Every test runs against a ``tmp_path`` settings file (or an injected env
mapping), so nothing touches the real ``~/.config``. The module-boundary
test runs a subprocess asserting that importing ``seestar_controller.ui``
/ ``seestar_controller.ui.state`` does NOT import ``gi`` (the widget layer
is lazy).
"""

import importlib.util
import json
import os
import subprocess
import sys

import pytest

from seestar_controller import ui
from seestar_controller.ui.state import (
    DEFAULT_CONFIG_DIR,
    DEFAULT_DARK_THEME,
    DEFAULT_KEY_PATH,
    DEFAULT_SETTINGS,
    DEFAULT_VIEW_ID,
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    MAX_WINDOW_COORD,
    MAX_WINDOW_SIZE,
    MIN_WINDOW_SIZE,
    SETTINGS_FILE_NAME,
    SETTINGS_KEYS,
    SETTINGS_PATH_ENV_VAR,
    SETTINGS_SCHEMA_VERSION,
    VIEW_IDS,
    VIEWS,
    AppState,
    SettingsValidationError,
    UIStateError,
    UnknownViewError,
    View,
    default_settings_path,
    load_state,
    resolve_settings_path,
    view_by_id,
)


@pytest.fixture()
def settings_file(tmp_path):
    """A settings file path inside tmp_path."""
    return tmp_path / "config" / "settings.json"


@pytest.fixture()
def state(settings_file):
    """An AppState rooted at the tmp settings file (defaults, not loaded)."""
    return AppState(settings_file)


# --- views + navigation ----------------------------------------------------------


def test_views_are_the_five_in_sidebar_order():
    assert VIEW_IDS == ("observe", "enhance", "photometry", "lightcurve", "projects")
    assert [v.id for v in VIEWS] == list(VIEW_IDS)
    assert [v.title for v in VIEWS] == [
        "Observe",
        "Enhance",
        "Photometry",
        "Lightcurve",
        "Projects",
    ]
    assert all(isinstance(v, View) for v in VIEWS)
    assert len({v.id for v in VIEWS}) == 5


def test_default_view_is_observe(state):
    assert DEFAULT_VIEW_ID == "observe"
    assert state.current_view_id == "observe"
    assert state.current_view == View("observe", "Observe")
    assert state.views == VIEWS


@pytest.mark.parametrize("view_id", VIEW_IDS)
def test_select_view_valid_returns_view_and_sets_current(state, view_id):
    selected = state.select_view(view_id)
    assert selected == view_by_id(view_id)
    assert state.current_view_id == view_id
    assert state.current_view.id == view_id


@pytest.mark.parametrize("bad", ["nope", "", "OBSERVE", "observe ", None, 42, ["observe"]])
def test_select_view_invalid_raises_and_keeps_current(state, bad):
    state.select_view("enhance")
    with pytest.raises(UnknownViewError) as excinfo:
        state.select_view(bad)
    assert isinstance(excinfo.value, ValueError)
    assert isinstance(excinfo.value, UIStateError)
    for view_id in VIEW_IDS:
        assert view_id in str(excinfo.value)
    assert state.current_view_id == "enhance"  # unchanged


def test_view_by_id_unknown_raises():
    with pytest.raises(UnknownViewError):
        view_by_id("settings")


# --- defaults --------------------------------------------------------------------


def test_defaults(state):
    assert state.dark_theme is True
    assert DEFAULT_DARK_THEME is True
    assert state.window_geometry() == {
        "width": DEFAULT_WINDOW_WIDTH,
        "height": DEFAULT_WINDOW_HEIGHT,
        "x": None,
        "y": None,
    }
    assert state.window_width == DEFAULT_WINDOW_WIDTH == 1280
    assert state.window_height == DEFAULT_WINDOW_HEIGHT == 800
    assert state.window_x is None and state.window_y is None
    assert state.settings == DEFAULT_SETTINGS
    assert state.get_setting("scope_ip") == ""
    assert state.get_setting("key_path") == str(DEFAULT_KEY_PATH)
    # unset on a fresh state — the project controller falls back to the
    # store default root (projects-view ticket 02)
    assert state.get_setting("project_root") == ""
    assert state.get_setting("photometry_comp_search_radius_deg") == "1.5"
    assert state.get_setting("last_project") == ""
    assert set(state.settings) == set(SETTINGS_KEYS) == {
        "scope_ip",
        "key_path",
        "project_root",
        "photometry_comp_search_radius_deg",
        "last_project",
    }


def test_settings_dict_property_returns_a_copy(state):
    state.settings["scope_ip"] = "mutated"
    assert state.get_setting("scope_ip") == ""


def test_to_dict_shape(state):
    state.select_view("lightcurve")
    state.set_window_geometry(1024, 700, 5, 6)
    state.set_dark_theme(False)
    state.set_setting("scope_ip", "192.168.1.50")
    data = state.to_dict()
    assert set(data) == {"schema_version", "current_view", "dark_theme", "window", "settings"}
    assert data["schema_version"] == SETTINGS_SCHEMA_VERSION == 1
    assert data["current_view"] == "lightcurve"
    assert data["dark_theme"] is False
    assert data["window"] == {"width": 1024, "height": 700, "x": 5, "y": 6}
    assert set(data["settings"]) == set(SETTINGS_KEYS)
    assert data["settings"]["scope_ip"] == "192.168.1.50"


# --- save / load round-trip -------------------------------------------------------


def test_save_load_round_trip(settings_file, state):
    state.select_view("projects")
    state.set_window_geometry(1024, 768, 12, -34)
    state.set_dark_theme(False)
    state.set_setting("scope_ip", "seestar.local")
    state.set_setting("key_path", "/tmp/keys/key.pem")
    state.set_setting("project_root", "/tmp/projects")
    written = state.save()

    assert written == settings_file
    assert settings_file.is_file()
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk == state.to_dict()

    loaded = AppState(settings_file).load()
    assert loaded.to_dict() == state.to_dict()
    assert loaded.current_view_id == "projects"
    assert loaded.window_geometry() == {"width": 1024, "height": 768, "x": 12, "y": -34}
    assert loaded.dark_theme is False
    assert loaded.get_setting("scope_ip") == "seestar.local"
    assert loaded.get_setting("key_path") == "/tmp/keys/key.pem"
    assert loaded.get_setting("project_root") == "/tmp/projects"


def test_save_creates_parent_dirs_and_load_state_helper(tmp_path):
    path = tmp_path / "deep" / "nested" / "settings.json"
    state = load_state(path)
    assert state.settings_path == path
    assert state.current_view_id == DEFAULT_VIEW_ID  # missing file -> defaults
    state.select_view("photometry")
    state.save()
    assert path.is_file()
    assert load_state(path).current_view_id == "photometry"


def test_load_and_save_accept_a_path_override(settings_file, tmp_path):
    state = AppState(settings_file)
    other = tmp_path / "other.json"
    state.select_view("enhance")
    state.save(other)
    assert other.is_file() and not settings_file.exists()
    assert state.settings_path == other
    fresh = AppState(settings_file)
    fresh.load(other)
    assert fresh.current_view_id == "enhance"
    assert fresh.settings_path == other


# --- atomic write ------------------------------------------------------------------


def test_save_leaves_no_tmp_leftovers(settings_file, state):
    state.save()
    state.select_view("enhance")
    state.save()
    files = sorted(p.name for p in settings_file.parent.iterdir())
    assert files == [SETTINGS_FILE_NAME]


def test_failed_atomic_write_keeps_original_and_leaves_no_tmp(
    settings_file, state, monkeypatch
):
    state.save()
    original = settings_file.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("simulated crash at rename")

    monkeypatch.setattr(os, "replace", boom)
    state.select_view("projects")
    with pytest.raises(OSError):
        state.save()
    monkeypatch.undo()

    assert settings_file.read_bytes() == original  # destination untouched
    files = sorted(p.name for p in settings_file.parent.iterdir())
    assert files == [SETTINGS_FILE_NAME]  # no *.tmp leftover
    # and the state is still loadable from the intact file
    assert AppState(settings_file).load().current_view_id == "observe"


# --- corrupt / missing settings file ------------------------------------------------


def test_load_missing_file_keeps_defaults_and_writes_nothing(settings_file):
    state = AppState(settings_file).load()
    assert state.to_dict() == AppState(settings_file).to_dict()
    assert not settings_file.exists()  # load never writes


@pytest.mark.parametrize(
    "payload",
    [
        b"not json {{{",
        b"",
        b"\xff\xfe binary garbage",
        json.dumps(["a", "list"]).encode(),
        json.dumps("a string").encode(),
        json.dumps({"schema_version": 999, "current_view": "enhance"}).encode(),
        json.dumps({"schema_version": "1"}).encode(),
    ],
)
def test_load_corrupt_file_returns_defaults_no_crash(settings_file, payload):
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_bytes(payload)
    state = AppState(settings_file).load()
    assert state.current_view_id == DEFAULT_VIEW_ID
    assert state.dark_theme is True
    assert state.window_geometry()["width"] == DEFAULT_WINDOW_WIDTH
    assert state.settings == DEFAULT_SETTINGS


def test_load_partial_fields_fall_back_per_field(settings_file):
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "current_view": "lightcurve",  # valid -> kept
                "dark_theme": "yes",  # not a bool -> default
                "window": {"width": "big", "height": 700, "x": 3, "y": None},
                "settings": {
                    "scope_ip": "not an ip!",  # invalid -> default
                    "key_path": "/tmp/k.pem",  # valid -> kept
                    "unknown_key": "ignored",
                },
            }
        ),
        encoding="utf-8",
    )
    state = AppState(settings_file).load()
    assert state.current_view_id == "lightcurve"
    assert state.dark_theme is True
    assert state.window_geometry() == {
        "width": DEFAULT_WINDOW_WIDTH,  # invalid -> default
        "height": 700,
        "x": 3,
        "y": None,
    }
    assert state.get_setting("scope_ip") == ""
    assert state.get_setting("key_path") == "/tmp/k.pem"
    # unset on a fresh state — the project controller falls back to the
    # store default root (projects-view ticket 02)
    assert state.get_setting("project_root") == ""
    assert set(state.settings) == set(SETTINGS_KEYS)  # unknown key not stored


# --- window geometry -----------------------------------------------------------------


def test_window_geometry_round_trip(settings_file, state):
    state.set_window_geometry(1440, 900, -100, 2048)
    state.save()
    loaded = AppState(settings_file).load()
    assert loaded.window_geometry() == {"width": 1440, "height": 900, "x": -100, "y": 2048}
    assert (loaded.window_width, loaded.window_height, loaded.window_x, loaded.window_y) == (
        1440,
        900,
        -100,
        2048,
    )


def test_window_geometry_unset_position_round_trip(settings_file, state):
    state.set_window_geometry(800, 600)
    state.save()
    loaded = AppState(settings_file).load()
    assert loaded.window_geometry() == {"width": 800, "height": 600, "x": None, "y": None}


@pytest.mark.parametrize(
    "width,height,x,y",
    [
        (0, 800, None, None),
        (MIN_WINDOW_SIZE - 1, 800, None, None),
        (MAX_WINDOW_SIZE + 1, 800, None, None),
        (True, 800, None, None),
        ("800", 800, None, None),
        (800, -5, None, None),
        (800, 800.5, None, None),
        (800, 800, 1.5, None),
        (800, 800, None, "10"),
        (800, 800, MAX_WINDOW_COORD + 1, None),
        (800, 800, None, -MAX_WINDOW_COORD - 1),
    ],
)
def test_set_window_geometry_validation(state, width, height, x, y):
    with pytest.raises(SettingsValidationError) as excinfo:
        state.set_window_geometry(width, height, x, y)
    assert isinstance(excinfo.value, ValueError)
    assert isinstance(excinfo.value, UIStateError)
    # unchanged
    assert state.window_geometry() == {
        "width": DEFAULT_WINDOW_WIDTH,
        "height": DEFAULT_WINDOW_HEIGHT,
        "x": None,
        "y": None,
    }


def test_set_window_geometry_bounds_ok(state):
    geo = state.set_window_geometry(
        MIN_WINDOW_SIZE, MAX_WINDOW_SIZE, -MAX_WINDOW_COORD, MAX_WINDOW_COORD
    )
    assert geo == {
        "width": MIN_WINDOW_SIZE,
        "height": MAX_WINDOW_SIZE,
        "x": -MAX_WINDOW_COORD,
        "y": MAX_WINDOW_COORD,
    }


# --- dark theme ------------------------------------------------------------------------


def test_dark_theme_default_true_and_round_trip(settings_file, state):
    assert state.dark_theme is True
    state.set_dark_theme(False)
    assert state.dark_theme is False
    state.save()
    assert AppState(settings_file).load().dark_theme is False


@pytest.mark.parametrize("bad", ["yes", 1, 0, None, [True]])
def test_set_dark_theme_rejects_non_bool(state, bad):
    with pytest.raises(SettingsValidationError):
        state.set_dark_theme(bad)
    assert state.dark_theme is True


# --- settings dict validation -------------------------------------------------------------


@pytest.mark.parametrize("good", ["", "192.168.1.50", "seestar.local", "scope-1", "10.0.0.2"])
def test_set_scope_ip_valid(state, good):
    assert state.set_setting("scope_ip", good) == good.strip()
    assert state.get_setting("scope_ip") == good.strip()


@pytest.mark.parametrize(
    "bad", ["not an ip!", "192.168.1.256", "999.1.1.1", "-bad-.", "a" * 254, 42, None, True]
)
def test_set_scope_ip_invalid(state, bad):
    with pytest.raises(SettingsValidationError):
        state.set_setting("scope_ip", bad)
    assert state.get_setting("scope_ip") == ""


@pytest.mark.parametrize("key", ["key_path", "project_root"])
@pytest.mark.parametrize("bad", [42, None, True, ["/tmp"]])
def test_path_settings_reject_non_str(state, key, bad):
    with pytest.raises(SettingsValidationError):
        state.set_setting(key, bad)


@pytest.mark.parametrize("key", ["key_path", "project_root"])
@pytest.mark.parametrize("empty", ["", "   "])
def test_path_settings_allow_empty_unset(settings_file, state, key, empty):
    # empty (unset) is allowed since ticket 02 and normalized to ""
    assert state.set_setting(key, empty) == ""
    assert state.get_setting(key) == ""
    state.save()
    assert AppState(settings_file).load().get_setting(key) == ""  # round-trips


def test_path_settings_accept_paths_and_round_trip(settings_file, state):
    state.set_setting("key_path", "/tmp/keys/key.pem")
    state.set_setting("project_root", "/mnt/data/SeestarProjects")
    state.save()
    loaded = AppState(settings_file).load()
    assert loaded.get_setting("key_path") == "/tmp/keys/key.pem"
    assert loaded.get_setting("project_root") == "/mnt/data/SeestarProjects"


# --- photometry comp-search radius setting (photometry ticket 02) ----------------------


def test_comp_search_radius_default_and_normalization(state):
    assert state.get_setting("photometry_comp_search_radius_deg") == "1.5"
    assert state.set_setting("photometry_comp_search_radius_deg", "2") == "2"
    assert state.set_setting("photometry_comp_search_radius_deg", 0.25) == "0.25"
    assert state.set_setting("photometry_comp_search_radius_deg", " 1.50 ") == "1.5"
    assert state.get_setting("photometry_comp_search_radius_deg") == "1.5"


@pytest.mark.parametrize(
    "bad", ["0", "-1", "0.0", "181", "180.5", "abc", "", "  ", float("nan"), float("inf"), 0, -2, 200, True, None, ["1"]]
)
def test_comp_search_radius_invalid(state, bad):
    with pytest.raises(SettingsValidationError):
        state.set_setting("photometry_comp_search_radius_deg", bad)
    assert state.get_setting("photometry_comp_search_radius_deg") == "1.5"  # unchanged


def test_comp_search_radius_round_trip(settings_file, state):
    state.set_setting("photometry_comp_search_radius_deg", "2.5")
    state.save()
    loaded = AppState(settings_file).load()
    assert loaded.get_setting("photometry_comp_search_radius_deg") == "2.5"
    # an old settings file without the key keeps the default
    settings_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "settings": {"scope_ip": "seestar.local", "unknown": "ignored"},
            }
        ),
        encoding="utf-8",
    )
    loaded = AppState(settings_file).load()
    assert loaded.get_setting("photometry_comp_search_radius_deg") == "1.5"
    assert loaded.get_setting("scope_ip") == "seestar.local"


def test_unknown_setting_key_raises(state):
    with pytest.raises(SettingsValidationError) as excinfo:
        state.set_setting("theme", "dark")
    for key in SETTINGS_KEYS:
        assert key in str(excinfo.value)
    with pytest.raises(SettingsValidationError):
        state.get_setting("nope")


# --- settings path resolution ---------------------------------------------------------------


def test_default_settings_path():
    assert default_settings_path({}) == DEFAULT_CONFIG_DIR / SETTINGS_FILE_NAME
    assert resolve_settings_path(None, env={}) == DEFAULT_CONFIG_DIR / SETTINGS_FILE_NAME
    assert resolve_settings_path() == default_settings_path()


def test_env_var_override(monkeypatch, tmp_path):
    env_file = tmp_path / "env-settings.json"
    monkeypatch.setenv(SETTINGS_PATH_ENV_VAR, str(env_file))
    assert default_settings_path() == env_file
    assert resolve_settings_path() == env_file
    assert AppState().settings_path == env_file
    # explicit arg beats the env var
    explicit = tmp_path / "explicit.json"
    assert resolve_settings_path(explicit) == explicit
    assert AppState(explicit).settings_path == explicit


def test_env_mapping_param_without_touching_os_environ(tmp_path):
    env_file = tmp_path / "from-mapping.json"
    assert default_settings_path({SETTINGS_PATH_ENV_VAR: str(env_file)}) == env_file
    assert resolve_settings_path(None, env={SETTINGS_PATH_ENV_VAR: str(env_file)}) == env_file
    assert AppState(env={SETTINGS_PATH_ENV_VAR: str(env_file)}).settings_path == env_file
    # an empty env value falls back to the default
    assert default_settings_path({SETTINGS_PATH_ENV_VAR: ""}) == (
        DEFAULT_CONFIG_DIR / SETTINGS_FILE_NAME
    )


def test_env_var_round_trip_via_env(monkeypatch, tmp_path):
    env_file = tmp_path / "settings.json"
    monkeypatch.setenv(SETTINGS_PATH_ENV_VAR, str(env_file))
    state = AppState()
    state.select_view("enhance")
    state.set_window_geometry(900, 700, 1, 2)
    state.save()
    assert env_file.is_file()
    assert AppState().load().current_view_id == "enhance"


# --- errors + public API ----------------------------------------------------------------------


def test_error_hierarchy():
    assert issubclass(UnknownViewError, UIStateError)
    assert issubclass(UnknownViewError, ValueError)
    assert issubclass(SettingsValidationError, UIStateError)
    assert issubclass(SettingsValidationError, ValueError)
    assert issubclass(UIStateError, Exception)


def test_public_api_is_exported_from_package():
    # pure-layer names are eagerly available (no gi involved) ...
    for name in ui.__all__:
        if name not in ui._WIDGET_EXPORTS:
            assert hasattr(ui, name), name
    # ... widget-layer names are listed but resolved lazily (they import gi)
    for name in ui._WIDGET_EXPORTS:
        assert name in ui.__all__
    assert callable(ui.load_state)
    assert callable(ui.resolve_settings_path)
    assert callable(ui.view_by_id)
    assert isinstance(ui.AppState, type)
    assert ui.VIEW_IDS == VIEW_IDS
    assert ui.SETTINGS_SCHEMA_VERSION == 1


# --- module boundary ------------------------------------------------------------------


def test_ui_package_imports_no_gi_and_widget_layer_stays_lazy():
    code = (
        "import sys;"
        "import seestar_controller.ui;"
        "import seestar_controller.ui.state;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.ui.app' not in sys.modules, 'app imported eagerly';"
        "assert 'seestar_controller.ui.views' not in sys.modules, 'views imported eagerly';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


# --- CLI gui subcommand without GTK ----------------------------------------------------


@pytest.mark.skipif(
    importlib.util.find_spec("gi") is not None,
    reason="gi is installed here — running `gui` would launch the real app",
)
def test_cli_gui_without_gtk_fails_cleanly(capsys):
    from seestar_controller import cli

    rc = cli.main(["gui"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "python3-gi" in err
    assert "gir1.2-gtk-4.0" in err
    assert "gir1.2-adw-1" in err
