"""Pure-Python settings-dialog controller tests — no gi, tmp_path only.

Covers the ticket-02 pure layer ``seestar_controller.ui.settings``:
``validate_settings`` (per-field hard errors, empty dict = valid),
``flag_settings`` (soft flags, e.g. a missing PEM key file — never blocking),
``SettingsFieldError`` (field + message + errors), and ``SettingsController``
(update -> validate -> apply -> atomic persist -> observer notify; invalid ->
nothing persisted). Every test runs against a ``tmp_path`` settings file, so
nothing touches the real ``~/.config``. The module-boundary test runs a
subprocess asserting that importing the pure ui modules does NOT import
``gi`` and does not pull in the widget layer (``settings_dialog``/``app``).
"""

import json
import subprocess
import sys

import pytest

from seestar_controller import ui
from seestar_controller.ui.settings import (
    SettingsController,
    SettingsFieldError,
    comp_search_radius_deg,
    flag_settings,
    validate_settings,
)
from seestar_controller.ui.state import (
    DEFAULT_COMP_SEARCH_RADIUS_DEG,
    DEFAULT_SETTINGS,
    SETTINGS_FILE_NAME,
    SETTINGS_KEYS,
    AppState,
    SettingsValidationError,
    UIStateError,
    load_state,
)


@pytest.fixture()
def settings_file(tmp_path):
    """A settings file path inside tmp_path."""
    return tmp_path / "config" / "settings.json"


@pytest.fixture()
def state(settings_file):
    """An AppState rooted at the tmp settings file (defaults, not loaded)."""
    return AppState(settings_file)


@pytest.fixture()
def controller(state):
    """A SettingsController over the tmp AppState."""
    return SettingsController(state)


@pytest.fixture()
def root_dir(tmp_path):
    """An existing directory usable as project_root."""
    root = tmp_path / "projects"
    root.mkdir()
    return root


# --- validate_settings: hard per-field errors, empty dict = valid -----------------


def test_validate_settings_empty_mapping_is_valid():
    assert validate_settings({}) == {}


def test_validate_settings_all_unset_is_valid():
    assert validate_settings({"scope_ip": "", "key_path": "", "project_root": ""}) == {}


# --- validate_settings: the photometry comp-search radius (photometry ticket 02) ------


@pytest.mark.parametrize("good", ["1.5", "2", 0.5, "0.1", "180", 180.0])
def test_validate_settings_comp_search_radius_valid(good):
    assert validate_settings({"photometry_comp_search_radius_deg": good}) == {}


@pytest.mark.parametrize("bad", ["0", "-1", "180.5", "abc", "", True, None])
def test_validate_settings_comp_search_radius_invalid(bad):
    errors = validate_settings({"photometry_comp_search_radius_deg": bad})
    assert set(errors) == {"photometry_comp_search_radius_deg"}
    assert "photometry_comp_search_radius_deg" in errors["photometry_comp_search_radius_deg"]


def test_comp_search_radius_deg_helper_reads_and_falls_back():
    assert comp_search_radius_deg(DEFAULT_SETTINGS) == DEFAULT_COMP_SEARCH_RADIUS_DEG == 0.5
    assert comp_search_radius_deg({"photometry_comp_search_radius_deg": "2.5"}) == 2.5
    assert comp_search_radius_deg({"photometry_comp_search_radius_deg": 0.25}) == 0.25
    # missing / invalid -> the default, never raises
    assert comp_search_radius_deg({}) == 0.5
    assert comp_search_radius_deg({"photometry_comp_search_radius_deg": "abc"}) == 0.5
    assert comp_search_radius_deg({"photometry_comp_search_radius_deg": "0"}) == 0.5


def test_update_comp_search_radius_persists_and_reloads(settings_file, controller, root_dir):
    controller.update(project_root=str(root_dir))
    new_values = controller.update(photometry_comp_search_radius_deg="2.5")
    assert new_values["photometry_comp_search_radius_deg"] == "2.5"
    assert comp_search_radius_deg(new_values) == 2.5
    reloaded = load_state(settings_file)
    assert reloaded.get_setting("photometry_comp_search_radius_deg") == "2.5"
    assert comp_search_radius_deg(reloaded.settings) == 2.5
    # invalid radius -> SettingsFieldError, nothing persisted
    before = settings_file.read_bytes()
    with pytest.raises(SettingsFieldError) as excinfo:
        controller.update(photometry_comp_search_radius_deg="-3")
    assert excinfo.value.field == "photometry_comp_search_radius_deg"
    assert settings_file.read_bytes() == before
    assert controller.values["photometry_comp_search_radius_deg"] == "2.5"


@pytest.mark.parametrize("good", ["", "192.168.1.50", "seestar.local", "scope-1", "10.0.0.2"])
def test_validate_settings_scope_ip_valid(good):
    assert validate_settings({"scope_ip": good}) == {}


@pytest.mark.parametrize("bad", ["not an ip!", "192.168.1.256", "999.1.1.1", "-bad-.", 42, None])
def test_validate_settings_scope_ip_invalid(bad):
    errors = validate_settings({"scope_ip": bad})
    assert set(errors) == {"scope_ip"}
    assert "scope_ip" in errors["scope_ip"]


def test_validate_settings_key_path_plausible_and_missing_file_is_not_an_error(tmp_path):
    # existence is NOT required at validation time (ticket 02)
    missing = tmp_path / "not-yet-extracted.pem"
    assert validate_settings({"key_path": str(missing)}) == {}
    assert validate_settings({"key_path": "/any/plausible/path.pem"}) == {}


@pytest.mark.parametrize("bad", [42, None, True, "has\0nul"])
def test_validate_settings_key_path_invalid(bad):
    errors = validate_settings({"key_path": bad})
    assert set(errors) == {"key_path"}


def test_validate_settings_project_root_must_be_existing_dir(tmp_path, root_dir):
    assert validate_settings({"project_root": str(root_dir)}) == {}
    missing = tmp_path / "no-such-dir"
    errors = validate_settings({"project_root": str(missing)})
    assert set(errors) == {"project_root"}
    assert "not an existing directory" in errors["project_root"]
    # a file is not a directory either
    a_file = tmp_path / "file.txt"
    a_file.write_text("x", encoding="utf-8")
    errors = validate_settings({"project_root": str(a_file)})
    assert set(errors) == {"project_root"}


def test_validate_settings_unknown_key_is_an_error():
    errors = validate_settings({"bogus": "x"})
    assert set(errors) == {"bogus"}
    assert "unknown setting" in errors["bogus"]


def test_validate_settings_checks_only_present_keys():
    errors = validate_settings({"scope_ip": "bad ip!"})
    assert set(errors) == {"scope_ip"}  # absent keys are not checked


def test_validate_settings_multiple_errors_at_once(root_dir):
    errors = validate_settings(
        {"scope_ip": "999.1.1.1", "key_path": 42, "project_root": "/no/such/dir"}
    )
    assert set(errors) == {"scope_ip", "key_path", "project_root"}


# --- flag_settings: soft flags, never blocking --------------------------------------


def test_flag_settings_missing_key_file_is_flagged(tmp_path):
    missing = tmp_path / "key.pem"
    flags = flag_settings({"key_path": str(missing)})
    assert set(flags) == {"key_path"}
    assert "not found" in flags["key_path"]
    assert "extract-key" in flags["key_path"]  # actionable hint


def test_flag_settings_existing_or_unset_key_path_not_flagged(tmp_path):
    key = tmp_path / "key.pem"
    key.write_text("x", encoding="utf-8")
    assert flag_settings({"key_path": str(key)}) == {}
    assert flag_settings({"key_path": ""}) == {}
    assert flag_settings({}) == {}


# --- SettingsFieldError ---------------------------------------------------------------


def test_settings_field_error_carries_field_message_errors():
    err = SettingsFieldError("scope_ip", "bad ip", {"scope_ip": "bad ip", "key_path": "x"})
    assert isinstance(err, SettingsValidationError)
    assert isinstance(err, UIStateError)
    assert isinstance(err, ValueError)
    assert err.field == "scope_ip"
    assert err.message == "bad ip"
    assert err.errors == {"scope_ip": "bad ip", "key_path": "x"}
    assert "scope_ip" in str(err) and "bad ip" in str(err)


def test_settings_field_error_defaults_errors_to_field():
    err = SettingsFieldError("project_root", "missing dir")
    assert err.errors == {"project_root": "missing dir"}


# --- SettingsController: values / validate / flags --------------------------------------


def test_controller_values_are_the_state_settings(controller):
    assert controller.values == DEFAULT_SETTINGS
    assert controller.values is not controller.state.settings  # a copy
    assert controller.validate() == validate_settings(DEFAULT_SETTINGS)
    assert controller.flags() == flag_settings(DEFAULT_SETTINGS)
    assert controller.state is not None
    assert controller.listeners == ()


def test_controller_validate_and_flags_accept_a_mapping(controller, root_dir):
    values = {"scope_ip": "bad!", "key_path": "/no/file.pem", "project_root": str(root_dir)}
    assert set(controller.validate(values)) == {"scope_ip"}
    assert set(controller.flags(values)) == {"key_path"}


# --- update: valid -> persisted + reloaded on a fresh AppState ---------------------------


def test_update_valid_persists_and_reloads(settings_file, controller, root_dir, tmp_path):
    key_path = tmp_path / "keys" / "key.pem"
    new_values = controller.update(
        scope_ip="192.168.1.50", key_path=str(key_path), project_root=str(root_dir)
    )
    assert new_values == {
        "scope_ip": "192.168.1.50",
        "key_path": str(key_path),
        "project_root": str(root_dir),
        "photometry_comp_search_radius_deg": "0.5",
        "last_project": "",
        "site_latitude": "",
        "site_longitude": "",
        "site_elevation_m": "",
        "site_timezone": "UTC",
    }
    assert controller.values == new_values
    # persisted atomically to the settings file
    assert settings_file.is_file()
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["settings"] == new_values
    # ... and reloaded on a fresh AppState
    reloaded = AppState(settings_file).load()
    assert reloaded.settings == new_values
    assert load_state(settings_file).settings == new_values


def test_update_partial_keeps_other_fields(controller, root_dir):
    controller.update(scope_ip="10.0.0.2", key_path="/tmp/k.pem", project_root=str(root_dir))
    new_values = controller.update(scope_ip="10.0.0.9")
    assert new_values == {
        "scope_ip": "10.0.0.9",
        "key_path": "/tmp/k.pem",
        "project_root": str(root_dir),
        "photometry_comp_search_radius_deg": "0.5",
        "last_project": "",
        "site_latitude": "",
        "site_longitude": "",
        "site_elevation_m": "",
        "site_timezone": "UTC",
    }


def test_update_unset_paths_round_trip(settings_file, controller, root_dir):
    controller.update(scope_ip="", key_path="", project_root=str(root_dir))
    assert controller.values["key_path"] == ""
    reloaded = AppState(settings_file).load()
    assert reloaded.get_setting("key_path") == ""
    assert reloaded.get_setting("scope_ip") == ""
    assert reloaded.get_setting("project_root") == str(root_dir)


def test_update_missing_key_file_saves_anyway_flagged_only(settings_file, controller, root_dir, tmp_path):
    missing_key = tmp_path / "missing.pem"
    # flagged ...
    assert set(controller.flags({"key_path": str(missing_key)})) == {"key_path"}
    # ... but NOT an error for saving
    new_values = controller.update(key_path=str(missing_key), project_root=str(root_dir))
    assert new_values["key_path"] == str(missing_key)
    assert AppState(settings_file).load().get_setting("key_path") == str(missing_key)
    assert set(controller.flags()) == {"key_path"}


def test_update_no_args_revalidates_persists_and_notifies(settings_file, controller, root_dir):
    controller.update(project_root=str(root_dir))  # make the default root valid
    seen = []
    controller.add_listener(seen.append)
    controller.update()
    assert seen == [controller.values]
    assert settings_file.is_file()


def test_update_leaves_no_tmp_leftovers(settings_file, controller, root_dir):
    controller.update(scope_ip="1.2.3.4", project_root=str(root_dir))
    controller.update(scope_ip="5.6.7.8", project_root=str(root_dir))
    files = sorted(p.name for p in settings_file.parent.iterdir())
    assert files == [SETTINGS_FILE_NAME]


# --- update: invalid -> SettingsFieldError naming field, nothing persisted ----------------


def test_update_invalid_ip_raises_field_error_and_persists_nothing(
    settings_file, controller, root_dir
):
    controller.update(scope_ip="192.168.1.50", project_root=str(root_dir))
    before = settings_file.read_bytes()
    with pytest.raises(SettingsFieldError) as excinfo:
        controller.update(scope_ip="999.1.1.1")
    err = excinfo.value
    assert err.field == "scope_ip"
    assert "scope_ip" in err.message
    assert set(err.errors) == {"scope_ip"}
    assert isinstance(err, SettingsValidationError) and isinstance(err, ValueError)
    # nothing persisted, state unchanged
    assert settings_file.read_bytes() == before
    assert controller.values["scope_ip"] == "192.168.1.50"


def test_update_missing_project_root_dir_raises_and_persists_nothing(
    settings_file, controller, tmp_path
):
    with pytest.raises(SettingsFieldError) as excinfo:
        controller.update(project_root=str(tmp_path / "no-such-dir"))
    err = excinfo.value
    assert err.field == "project_root"
    assert "not an existing directory" in err.message
    assert not settings_file.exists()  # nothing persisted at all
    assert controller.values["project_root"] == DEFAULT_SETTINGS["project_root"]


def test_update_non_string_path_raises(controller, root_dir):
    controller.update(project_root=str(root_dir))
    with pytest.raises(SettingsFieldError) as excinfo:
        controller.update(key_path=42)
    assert excinfo.value.field == "key_path"


def test_update_invalid_does_not_notify_listeners(controller, root_dir):
    controller.update(project_root=str(root_dir))
    seen = []
    controller.add_listener(seen.append)
    with pytest.raises(SettingsFieldError):
        controller.update(scope_ip="bad ip!")
    assert seen == []


# --- observer listeners --------------------------------------------------------------------


def test_observer_notified_on_change_with_a_copy(controller, root_dir):
    seen = []
    controller.add_listener(seen.append)
    new_values = controller.update(scope_ip="10.1.2.3", project_root=str(root_dir))
    assert seen == [new_values]
    seen[0]["scope_ip"] = "mutated"  # listeners get a copy
    assert controller.values["scope_ip"] == "10.1.2.3"


def test_multiple_listeners_and_remove(controller, root_dir):
    calls_a, calls_b = [], []
    controller.add_listener(calls_a.append)
    controller.add_listener(calls_b.append)
    controller.add_listener(calls_a.append)  # idempotent
    assert len(controller.listeners) == 2
    controller.update(scope_ip="10.0.0.1", project_root=str(root_dir))
    assert calls_a == calls_b == [controller.values]
    controller.remove_listener(calls_a.append)
    controller.update(scope_ip="10.0.0.2", project_root=str(root_dir))
    assert len(calls_a) == 1 and len(calls_b) == 2
    controller.remove_listener(calls_a.append)  # no-op when absent
    assert len(controller.listeners) == 1


def test_add_listener_returns_the_listener_for_decorator_use(controller):
    @controller.add_listener
    def on_change(values):
        on_change.seen = values

    assert on_change in controller.listeners


# --- defaults when no config file + round-trip ----------------------------------------------


def test_defaults_when_no_config_file(settings_file, controller):
    assert not settings_file.exists()
    loaded = load_state(settings_file)
    assert loaded.settings == DEFAULT_SETTINGS
    assert not settings_file.exists()  # load never writes
    assert controller.values == DEFAULT_SETTINGS
    assert set(controller.values) == set(SETTINGS_KEYS) == {
        "scope_ip",
        "key_path",
        "project_root",
        "photometry_comp_search_radius_deg",
        "last_project",
        "site_latitude",
        "site_longitude",
        "site_elevation_m",
        "site_timezone",
    }


def test_round_trip_update_save_fresh_load(settings_file, controller, root_dir, tmp_path):
    key_path = tmp_path / "key.pem"
    key_path.write_text("PEM", encoding="utf-8")
    controller.update(
        scope_ip="seestar.local", key_path=str(key_path), project_root=str(root_dir)
    )
    fresh = load_state(settings_file)
    assert fresh.settings == controller.values
    assert fresh.to_dict()["settings"] == controller.values
    # a second controller over the reloaded state sees the same values
    assert SettingsController(fresh).values == controller.values


# --- public API + module boundary -------------------------------------------------------------


def test_public_api_is_exported_from_package():
    for name in (
        "SettingsController",
        "SettingsFieldError",
        "SettingsListener",
        "validate_settings",
        "flag_settings",
    ):
        assert name in ui.__all__
        assert hasattr(ui, name), name
    assert "SettingsDialog" in ui.__all__ and "SettingsDialog" in ui._WIDGET_EXPORTS
    assert ui.SettingsController is SettingsController
    assert ui.validate_settings is validate_settings
    assert ui.flag_settings is flag_settings
    assert issubclass(ui.SettingsFieldError, ui.SettingsValidationError)


def test_pure_ui_modules_import_no_gi_and_widget_layer_stays_lazy():
    code = (
        "import sys;"
        "import seestar_controller.ui;"
        "import seestar_controller.ui.state;"
        "import seestar_controller.ui.settings;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert 'seestar_controller.ui.app' not in sys.modules, 'app imported eagerly';"
        "assert 'seestar_controller.ui.views' not in sys.modules, 'views imported eagerly';"
        "assert 'seestar_controller.ui.settings_dialog' not in sys.modules, "
        "'settings_dialog imported eagerly';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout
