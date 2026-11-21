"""Pure-Python tests for the recent-projects list (offline-analysis 03) —
no gi, tmp_path only.

Every test runs against a ``tmp_path`` recent file (or an injected env
mapping), so nothing touches the real ``~/.config``. Covered: persistence
round-trip (atomic write, no ``*.tmp`` leftover), most-recent-first order,
dedup by path (with name/offline refresh), the cap, robustness (missing /
corrupt / wrong-schema file -> empty, malformed entries skipped), the
env-var path override, validation errors, remove/clear, the observer
notifications, and a subprocess test asserting ``seestar_controller.ui.recent``
never imports ``gi``.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from seestar_controller.ui.recent import (
    DEFAULT_RECENT_CAP,
    RECENT_FILE_NAME,
    RECENT_PATH_ENV_VAR,
    RECENT_SCHEMA_VERSION,
    RecentEntry,
    RecentProjects,
    RecentProjectsError,
    default_recent_path,
    load_recent,
    record_recent,
    resolve_recent_path,
    save_recent,
)
from seestar_controller.ui.state import DEFAULT_CONFIG_DIR, UIStateError


@pytest.fixture()
def recent_file(tmp_path):
    """A recent-projects file path inside tmp_path."""
    return tmp_path / RECENT_FILE_NAME


# --- path resolution ---------------------------------------------------------------


def test_default_recent_path_is_the_config_dir_file():
    assert default_recent_path({}) == DEFAULT_CONFIG_DIR / RECENT_FILE_NAME
    assert default_recent_path({RECENT_PATH_ENV_VAR: "/tmp/x/recent.json"}) == Path(
        "/tmp/x/recent.json"
    )


def test_resolve_recent_path_precedence_explicit_over_env_over_default(tmp_path):
    env = {RECENT_PATH_ENV_VAR: str(tmp_path / "env.json")}
    assert resolve_recent_path(str(tmp_path / "explicit.json"), env=env) == (
        tmp_path / "explicit.json"
    )
    assert resolve_recent_path(None, env=env) == tmp_path / "env.json"
    assert resolve_recent_path(None, env={}) == DEFAULT_CONFIG_DIR / RECENT_FILE_NAME


# --- persistence round-trip ----------------------------------------------------------


def test_save_load_round_trip(recent_file):
    entries = (
        RecentEntry(path="/projects/m-42", name="M 42", offline=True),
        RecentEntry(path="/projects/m-31", name="M 31"),
    )
    written = save_recent(entries, recent_file)
    assert written == recent_file
    assert recent_file.exists()
    # on-disk shape: schema_version + recent list
    obj = json.loads(recent_file.read_text(encoding="utf-8"))
    assert obj["schema_version"] == RECENT_SCHEMA_VERSION
    assert obj["recent"] == [
        {"path": "/projects/m-42", "name": "M 42", "offline": True},
        {"path": "/projects/m-31", "name": "M 31", "offline": False},
    ]
    assert load_recent(recent_file) == entries


def test_save_creates_parent_dirs_and_leaves_no_tmp(recent_file, tmp_path):
    nested = tmp_path / "a" / "b" / RECENT_FILE_NAME
    save_recent((RecentEntry(path="/p", name="P"),), nested)
    assert nested.exists()
    files = sorted(p.name for p in nested.parent.iterdir())
    assert files == [RECENT_FILE_NAME]  # no *.tmp leftover


def test_failed_atomic_write_keeps_original_and_leaves_no_tmp(recent_file, monkeypatch):
    save_recent((RecentEntry(path="/p", name="P"),), recent_file)
    original = recent_file.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("simulated crash at rename")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        save_recent((RecentEntry(path="/q", name="Q"),), recent_file)
    monkeypatch.undo()

    assert recent_file.read_bytes() == original  # destination untouched
    files = sorted(p.name for p in recent_file.parent.iterdir())
    assert files == [RECENT_FILE_NAME]  # no *.tmp leftover
    assert load_recent(recent_file) == (RecentEntry(path="/p", name="P"),)


# --- robustness: missing / corrupt / wrong schema --------------------------------------


def test_load_missing_file_returns_empty_and_writes_nothing(recent_file):
    assert load_recent(recent_file) == ()
    assert not recent_file.exists()


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all {{{",
        "[1, 2, 3]",
        '{"schema_version": 999, "recent": []}',
        '{"schema_version": 1, "recent": {"not": "a list"}}',
        '{"schema_version": 1}',
    ],
)
def test_load_corrupt_or_wrong_shape_returns_empty(recent_file, payload):
    recent_file.write_text(payload, encoding="utf-8")
    assert load_recent(recent_file) == ()


def test_load_skips_malformed_entries_but_keeps_valid_ones(recent_file):
    recent_file.write_text(
        json.dumps(
            {
                "schema_version": RECENT_SCHEMA_VERSION,
                "recent": [
                    {"path": "/good", "name": "Good", "offline": False},
                    {"name": "no path"},
                    {"path": ""},
                    "not an object",
                    {"path": "/bad-flag", "offline": "yes"},
                ],
            }
        ),
        encoding="utf-8",
    )
    assert load_recent(recent_file) == (RecentEntry(path="/good", name="Good"),)


def test_load_dedups_and_truncates_to_cap(recent_file):
    items = [{"path": f"/p{i}", "name": f"P{i}", "offline": False} for i in range(5)]
    items.append({"path": "/p0", "name": "P0-dup", "offline": True})  # duplicate path
    recent_file.write_text(
        json.dumps({"schema_version": RECENT_SCHEMA_VERSION, "recent": items}),
        encoding="utf-8",
    )
    assert load_recent(recent_file, cap=3) == tuple(
        RecentEntry(path=f"/p{i}", name=f"P{i}") for i in range(3)
    )


# --- record_recent: dedup, MRU order, cap ------------------------------------------------


def test_record_recent_puts_new_entry_first():
    entries = (RecentEntry(path="/a", name="A"), RecentEntry(path="/b", name="B"))
    result = record_recent(entries, "/c", "C")
    assert [e.path for e in result] == ["/c", "/a", "/b"]
    assert result[0] == RecentEntry(path="/c", name="C", offline=False)


def test_record_recent_dedups_by_path_and_refreshes_name_and_offline():
    entries = (RecentEntry(path="/a", name="A"), RecentEntry(path="/b", name="B"))
    result = record_recent(entries, "/b", "B2", offline=True)
    assert [e.path for e in result] == ["/b", "/a"]  # moved to front, no duplicate
    assert result[0] == RecentEntry(path="/b", name="B2", offline=True)


def test_record_recent_normalizes_path_forms(tmp_path):
    # str with a trailing slash and the equivalent Path dedup to one entry
    entries = record_recent((), str(tmp_path / "proj") + "/", "P")
    result = record_recent(entries, tmp_path / "proj", "P2")
    assert len(result) == 1
    assert result[0].path == str(tmp_path / "proj")
    assert result[0].name == "P2"


def test_record_recent_caps_the_list():
    entries: tuple[RecentEntry, ...] = ()
    for i in range(DEFAULT_RECENT_CAP + 3):
        entries = record_recent(entries, f"/p{i}", f"P{i}")
    assert len(entries) == DEFAULT_RECENT_CAP
    # most-recent-first: the last recorded is first, the oldest fell off
    assert entries[0].path == f"/p{DEFAULT_RECENT_CAP + 2}"
    assert entries[-1].path == "/p3"


def test_record_recent_custom_cap():
    entries = record_recent(record_recent((), "/a", "A"), "/b", "B", cap=1)
    assert entries == (RecentEntry(path="/b", name="B"),)


def test_record_recent_rejects_invalid_path_and_cap():
    with pytest.raises(RecentProjectsError):
        record_recent((), "")
    with pytest.raises(RecentProjectsError):
        record_recent((), 123)  # ty: ignore[invalid-argument-type]
    with pytest.raises(RecentProjectsError):
        record_recent((), "/a", "A", cap=0)
    with pytest.raises(RecentProjectsError):
        record_recent((), "/a", 42)  # ty: ignore[invalid-argument-type]
    # RecentProjectsError is both a UIStateError and a ValueError
    with pytest.raises(UIStateError):
        record_recent((), "")
    with pytest.raises(ValueError):
        record_recent((), "")


# --- RecentProjects class ----------------------------------------------------------------


def test_class_load_missing_file_gives_empty_list(recent_file):
    recent = RecentProjects.load(recent_file)
    assert recent.path == recent_file
    assert recent.cap == DEFAULT_RECENT_CAP
    assert recent.entries == ()


def test_class_record_persists_and_notifies(recent_file):
    recent = RecentProjects.load(recent_file)
    seen: list[tuple[RecentEntry, ...]] = []
    recent.add_listener(seen.append)
    entry = recent.record("/projects/m-42", "M 42", offline=True)
    assert entry == RecentEntry(path="/projects/m-42", name="M 42", offline=True)
    assert recent.entries == (entry,)
    assert seen == [(entry,)]  # listener notified with the new entries
    # persisted: a fresh load from the same file sees it
    assert RecentProjects.load(recent_file).entries == (entry,)
    assert load_recent(recent_file) == (entry,)


def test_class_record_dedups_and_caps(recent_file):
    recent = RecentProjects.load(recent_file, cap=2)
    recent.record("/a", "A")
    recent.record("/b", "B")
    recent.record("/a", "A2", offline=True)  # dedup: moves to front, refreshes
    assert [e.path for e in recent.entries] == ["/a", "/b"]
    assert recent.entries[0].offline is True
    recent.record("/c", "C")  # cap=2: /b falls off
    assert [e.path for e in recent.entries] == ["/c", "/a"]
    assert [e.path for e in load_recent(recent_file)] == ["/c", "/a"]


def test_class_entry_for_and_remove_and_clear(recent_file):
    recent = RecentProjects.load(recent_file)
    recent.record("/a", "A")
    recent.record("/b", "B", offline=True)
    assert recent.entry_for("/a") == RecentEntry(path="/a", name="A")
    assert recent.entry_for("/missing") is None
    assert recent.remove("/a") is True
    assert recent.remove("/a") is False  # already gone
    assert [e.path for e in recent.entries] == ["/b"]
    assert [e.path for e in load_recent(recent_file)] == ["/b"]
    recent.clear()
    assert recent.entries == ()
    assert load_recent(recent_file) == ()


def test_class_listener_remove_and_idempotent_add(recent_file):
    recent = RecentProjects.load(recent_file)
    seen: list[tuple[RecentEntry, ...]] = []
    recent.add_listener(seen.append)
    recent.add_listener(seen.append)  # idempotent
    assert len(recent.listeners) == 1
    recent.record("/a", "A")
    assert len(seen) == 1
    recent.remove_listener(seen.append)
    recent.record("/b", "B")
    assert len(seen) == 1  # no longer notified


def test_class_rejects_invalid_cap(recent_file):
    with pytest.raises(RecentProjectsError):
        RecentProjects(recent_file, cap=0)
    with pytest.raises(RecentProjectsError):
        RecentProjects.load(recent_file, cap=-1)


def test_class_save_to_path_override(recent_file, tmp_path):
    recent = RecentProjects(recent_file, entries=(RecentEntry(path="/a", name="A"),))
    other = tmp_path / "other.json"
    recent.save(other)
    assert load_recent(other) == recent.entries
    assert not recent_file.exists()  # the default path was not written


# --- module boundary: no gi ---------------------------------------------------------------


def test_recent_module_imports_no_gi_in_a_subprocess():
    code = (
        "import sys;"
        "import seestar_controller.ui.recent;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout
