"""Pure-Python recent-projects list for the ui shell (offline-analysis 03).

This is part of the PURE layer of :mod:`seestar_controller.ui`: **no ``gi``
import anywhere in this module** — stdlib only — so it is fully unit-testable
headless (a subprocess test in tests/test_ui_harness.py enforces the
boundary). The widget layer (:mod:`.app`) imports it and renders the list as
the app menu's "Recent projects" submenu.

What lives here:

- :class:`RecentEntry` — one recently opened project: its ``path`` (as a
  string, the dedup key), its display ``name``, and whether it was last
  opened ``offline`` (reopening then uses the store-only offline path);
- persistence: one JSON file in the app config dir —
  ``~/.config/seestar-controller/recent.json`` by default (the same dir as
  ``settings.json``; the window passes the settings file's sibling, so a
  custom ``SEESTAR_SETTINGS_PATH`` keeps the recent list next to it), or
  ``$SEESTAR_RECENT_PATH`` when set — the same explicit-arg > env-var >
  default precedence as the settings path. Writes are atomic (temp file in
  the same dir + fsync + :func:`os.replace`, the same pattern as
  :func:`.state._atomic_write_json` — re-declared here because private
  helpers are not shared), so a crash never leaves a partial file and no
  ``*.tmp`` leftover survives a failed write;
- the list semantics: most-recent-first, deduplicated by path (re-opening a
  project moves it to the front and refreshes its name/offline flag), capped
  at :data:`DEFAULT_RECENT_CAP` entries;
- robustness (documented choice, same as :meth:`.state.AppState.load`):
  :func:`load_recent` NEVER raises for a missing, unreadable, corrupt, or
  wrongly-shaped file — it returns the empty list; individual malformed
  entries are skipped.

On-disk shape (``schema_version`` :data:`RECENT_SCHEMA_VERSION`)::

    {
      "schema_version": 1,
      "recent": [
        {"path": "/home/.../SeestarProjects/m-42", "name": "M 42", "offline": true}
      ]
    }
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state import DEFAULT_CONFIG_DIR, UIStateError

#: File name of the recent-projects list inside the app config dir.
RECENT_FILE_NAME = "recent.json"

#: Environment variable that overrides the default recent-projects path (same
#: explicit-arg > env-var > default precedence as the settings path).
RECENT_PATH_ENV_VAR = "SEESTAR_RECENT_PATH"

#: Version of the on-disk recent-projects schema; a file with any other
#: version is treated as corrupt and falls back to the empty list.
RECENT_SCHEMA_VERSION = 1

#: Default maximum number of remembered projects (the list is capped).
DEFAULT_RECENT_CAP = 10


class RecentProjectsError(UIStateError, ValueError):
    """A recent-projects operation failed (invalid path, invalid cap, ...).

    Subclasses both :class:`~.state.UIStateError` and :class:`ValueError`.
    """


@dataclass(frozen=True)
class RecentEntry:
    """One recently opened project.

    - ``path`` — the project directory as a string (the dedup key);
    - ``name`` — the display name (may be empty when unknown);
    - ``offline`` — whether it was last opened offline (reopening then uses
      the store-only offline path, not the normal open).
    """

    path: str
    name: str = ""
    offline: bool = False

    def to_dict(self) -> dict[str, Any]:
        """The JSON-serializable form (see the module docstring)."""
        return {"path": self.path, "name": self.name, "offline": self.offline}

    @classmethod
    def from_dict(cls, obj: Any) -> RecentEntry:
        """Parse one entry; raises :class:`RecentProjectsError` on a bad shape."""
        if not isinstance(obj, dict):
            raise RecentProjectsError(f"recent entry is not an object: {obj!r}")
        path = obj.get("path")
        if not isinstance(path, str) or not path:
            raise RecentProjectsError(f"recent entry has no usable path: {obj!r}")
        name = obj.get("name", "")
        if not isinstance(name, str):
            raise RecentProjectsError(f"recent entry name is not a string: {obj!r}")
        offline = obj.get("offline", False)
        if not isinstance(offline, bool):
            raise RecentProjectsError(f"recent entry offline flag is not a bool: {obj!r}")
        return cls(path=path, name=name, offline=offline)


def default_recent_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the default recent-projects path.

    Precedence: the ``SEESTAR_RECENT_PATH`` environment variable > the
    default ``~/.config/seestar-controller/recent.json`` (the app config
    dir, reusing :data:`.state.DEFAULT_CONFIG_DIR`).
    """
    environ = os.environ if env is None else env
    value = environ.get(RECENT_PATH_ENV_VAR)
    if value:
        return Path(value)
    return DEFAULT_CONFIG_DIR / RECENT_FILE_NAME


def resolve_recent_path(
    path: str | Path | None = None, *, env: Mapping[str, str] | None = None
) -> Path:
    """Resolve the recent-projects file path.

    Precedence: explicit ``path`` > ``SEESTAR_RECENT_PATH`` env var > the
    default ``~/.config/seestar-controller/recent.json``.
    """
    if path is not None:
        return Path(path)
    return default_recent_path(env)


def _normalize_path(path: str | Path) -> str:
    """Normalize a project path to its dedup/storage string form.

    Raises :class:`RecentProjectsError` for a non-string/empty path.
    """
    if isinstance(path, Path):
        value = str(path)
    elif isinstance(path, str):
        value = str(Path(path))
    else:
        raise RecentProjectsError(f"project path must be a str or Path, got {path!r}")
    if not value or value == ".":
        raise RecentProjectsError(f"project path must be non-empty, got {path!r}")
    return value


def _atomic_write_json(path: Path, obj: Any) -> Path:
    """Write ``obj`` as pretty JSON to ``path`` atomically.

    Same pattern as :func:`.state._atomic_write_json` (re-declared — private
    helpers are not shared): temp file in the same directory, flush + fsync,
    then :func:`os.replace` onto the destination. On any failure the temp
    file is removed and the destination is left untouched. Returns ``path``.
    """
    data = (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.close(fd)  # already closed when the with-block exited
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def load_recent(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    cap: int = DEFAULT_RECENT_CAP,
) -> tuple[RecentEntry, ...]:
    """Load the recent-projects list from ``path`` (resolved via
    :func:`resolve_recent_path` when ``None``).

    NEVER raises for a missing, unreadable, corrupt, or wrongly-shaped file
    — returns the empty tuple; malformed individual entries are skipped.
    The result is most-recent-first and truncated to ``cap``.
    """
    if cap < 1:
        raise RecentProjectsError(f"cap must be >= 1, got {cap!r}")
    file = resolve_recent_path(path, env=env)
    try:
        raw = file.read_text(encoding="utf-8")
    except OSError:
        return ()
    try:
        obj = json.loads(raw)
    except ValueError:
        return ()
    if not isinstance(obj, dict) or obj.get("schema_version") != RECENT_SCHEMA_VERSION:
        return ()
    items = obj.get("recent")
    if not isinstance(items, list):
        return ()
    entries: list[RecentEntry] = []
    seen: set[str] = set()
    for item in items:
        try:
            entry = RecentEntry.from_dict(item)
        except RecentProjectsError:
            continue  # skip malformed entries, keep the rest
        if entry.path in seen:
            continue  # dedup defensively (writers never emit duplicates)
        seen.add(entry.path)
        entries.append(entry)
        if len(entries) >= cap:
            break
    return tuple(entries)


def save_recent(
    entries: Sequence[RecentEntry],
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Persist ``entries`` (most-recent-first) atomically to ``path``
    (resolved via :func:`resolve_recent_path` when ``None``); returns the
    written path."""
    file = resolve_recent_path(path, env=env)
    obj = {
        "schema_version": RECENT_SCHEMA_VERSION,
        "recent": [entry.to_dict() for entry in entries],
    }
    return _atomic_write_json(file, obj)


def record_recent(
    entries: Sequence[RecentEntry],
    path: str | Path,
    name: str = "",
    *,
    offline: bool = False,
    cap: int = DEFAULT_RECENT_CAP,
) -> tuple[RecentEntry, ...]:
    """Pure list operation: return ``entries`` with ``path`` recorded as the
    most recent entry.

    Dedup by path: an existing entry for ``path`` is removed and re-inserted
    at the front with the refreshed ``name`` / ``offline`` flag. The result
    is most-recent-first and truncated to ``cap``. Raises
    :class:`RecentProjectsError` for an invalid path or cap.
    """
    if cap < 1:
        raise RecentProjectsError(f"cap must be >= 1, got {cap!r}")
    key = _normalize_path(path)
    if not isinstance(name, str):
        raise RecentProjectsError(f"name must be a str, got {name!r}")
    entry = RecentEntry(path=key, name=name, offline=bool(offline))
    rest = [item for item in entries if item.path != key]
    return (entry, *rest)[:cap]


#: A recent-list listener: called with the new entries tuple after every
#: change (in registration order).
RecentListener = Callable[[tuple[RecentEntry, ...]], None]


class RecentProjects:
    """Observer-able recent-projects list bound to one file.

    - :attr:`path` / :attr:`cap` / :attr:`entries` — the file, the cap, and
      the current most-recent-first entries;
    - :meth:`record` — record a project as most recent (dedup + cap + save +
      notify); :meth:`remove` / :meth:`clear` — drop entries (save + notify);
    - :meth:`load` — construct from the file (missing/corrupt -> empty);
    - :meth:`add_listener` / :meth:`remove_listener` — observers (the app
      menu rebuilds itself on every change).
    """

    def __init__(
        self,
        path: str | Path,
        *,
        cap: int = DEFAULT_RECENT_CAP,
        entries: Sequence[RecentEntry] = (),
    ) -> None:
        if cap < 1:
            raise RecentProjectsError(f"cap must be >= 1, got {cap!r}")
        self._path = Path(path)
        self._cap = cap
        self._entries: tuple[RecentEntry, ...] = tuple(entries)[:cap]
        self._listeners: list[RecentListener] = []

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
        cap: int = DEFAULT_RECENT_CAP,
    ) -> RecentProjects:
        """Construct from the file at ``path`` (resolved via
        :func:`resolve_recent_path` when ``None``); a missing/corrupt file
        yields an empty list (never raises)."""
        file = resolve_recent_path(path, env=env)
        return cls(file, cap=cap, entries=load_recent(file, env=env, cap=cap))

    # -- reading ---------------------------------------------------------------

    @property
    def path(self) -> Path:
        """The file the list is persisted to."""
        return self._path

    @property
    def cap(self) -> int:
        """The maximum number of remembered projects."""
        return self._cap

    @property
    def entries(self) -> tuple[RecentEntry, ...]:
        """The current entries, most-recent-first."""
        return self._entries

    @property
    def listeners(self) -> tuple[RecentListener, ...]:
        """The registered observer callbacks, in registration order."""
        return tuple(self._listeners)

    def entry_for(self, path: str | Path) -> RecentEntry | None:
        """The entry for ``path`` (dedup key match), or ``None``."""
        key = _normalize_path(path)
        for entry in self._entries:
            if entry.path == key:
                return entry
        return None

    # -- listeners ---------------------------------------------------------------

    def add_listener(self, listener: RecentListener) -> RecentListener:
        """Register an observer callback (idempotent); returns it, so it can
        be used as a decorator."""
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def remove_listener(self, listener: RecentListener) -> None:
        """Unregister a previously registered observer callback."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener(self._entries)

    # -- mutation ---------------------------------------------------------------

    def record(self, path: str | Path, name: str = "", *, offline: bool = False) -> RecentEntry:
        """Record ``path`` as the most recent project (dedup by path, cap,
        persist, notify); returns the new entry."""
        self._entries = record_recent(
            self._entries, path, name, offline=offline, cap=self._cap
        )
        entry = self._entries[0]
        self.save()
        self._notify()
        return entry

    def remove(self, path: str | Path) -> bool:
        """Drop the entry for ``path`` (persist + notify); returns whether an
        entry was removed."""
        key = _normalize_path(path)
        remaining = tuple(entry for entry in self._entries if entry.path != key)
        if len(remaining) == len(self._entries):
            return False
        self._entries = remaining
        self.save()
        self._notify()
        return True

    def clear(self) -> None:
        """Drop all entries (persist + notify)."""
        if not self._entries:
            return
        self._entries = ()
        self.save()
        self._notify()

    def save(self, path: str | Path | None = None) -> Path:
        """Persist the current entries atomically (to ``path`` when given,
        else :attr:`path`); returns the written path."""
        file = Path(path) if path is not None else self._path
        return save_recent(self._entries, file)
