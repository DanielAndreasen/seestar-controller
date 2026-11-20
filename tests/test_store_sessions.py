"""Project store: session lifecycle (start / close / list / get_open) — tmp_path, no network.

Every test runs against a ``tmp_path`` project root. Clock injection uses a
frozen ``clock=`` callable for deterministic session ids and timestamps.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from seestar_controller.store import (
    ProjectError,
    Session,
    SessionAlreadyClosedError,
    SessionNotFoundError,
    SessionOpenError,
    close_session,
    create_project,
    get_open_session,
    list_sessions,
    open_project,
    start_session,
)


@pytest.fixture()
def root(tmp_path):
    """A fresh project root."""
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    """One created project, closed after the test."""
    p = create_project("M 42", root=root)
    yield p
    p.close()


def frozen(dt: datetime):
    """A frozen clock returning ``dt`` on every call."""
    return lambda: dt


T0 = datetime(2026, 11, 20, 22, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1, minutes=30)


# --- start_session ---------------------------------------------------------------


def test_start_appears_in_list_with_start_and_no_end(project):
    session_id = start_session(project, clock=frozen(T0))
    sessions = list_sessions(project)
    assert len(sessions) == 1
    s = sessions[0]
    assert isinstance(s, Session)
    assert s.id == session_id
    assert s.started_at == "2026-11-20T22:00:00.000+00:00"
    assert s.ended_at is None
    # raw row: ended_at is NULL in the db
    row = project.db.execute(
        "SELECT id, started_at, ended_at FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    assert row == (session_id, "2026-11-20T22:00:00.000+00:00", None)


def test_start_returns_timestamp_slug_id(project):
    """Documented id choice: UTC ISO-8601 basic-format slug, e.g. 20261120T220000000000Z."""
    session_id = start_session(project, clock=frozen(T0))
    assert session_id == "20261120T220000000000Z"
    close_session(project, session_id, clock=frozen(T1))
    # microseconds are part of the slug
    with_micro = T0.replace(microsecond=123456)
    session_id2 = start_session(project, clock=frozen(with_micro))
    assert session_id2 == "20261120T220000123456Z"


def test_start_default_clock_is_utc_now(project):
    session_id = start_session(project)
    s = list_sessions(project)[0]
    parsed = datetime.fromisoformat(s.started_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)
    assert parsed <= datetime.now(timezone.utc) + timedelta(seconds=5)
    # the id slug matches the stored start
    assert session_id == s.id
    assert session_id.endswith("Z") and "T" in session_id


def test_start_while_open_is_rejected(project):
    first = start_session(project, clock=frozen(T0))
    with pytest.raises(SessionOpenError) as excinfo:
        start_session(project, clock=frozen(T1))
    assert first in str(excinfo.value)  # names the open session
    assert issubclass(SessionOpenError, ProjectError)
    # nothing was written: still exactly one (open) session
    sessions = list_sessions(project)
    assert [s.id for s in sessions] == [first]
    assert get_open_session(project).id == first


def test_start_after_close_is_allowed(project):
    first = start_session(project, clock=frozen(T0))
    close_session(project, first, clock=frozen(T1))
    second = start_session(project, clock=frozen(T1 + timedelta(hours=2)))
    assert second != first
    assert [s.id for s in list_sessions(project)] == [first, second]
    assert get_open_session(project).id == second


def test_start_id_collision_gets_suffix(project):
    """Same frozen clock twice (after a close) -> deterministic -2 suffix."""
    first = start_session(project, clock=frozen(T0))
    close_session(project, first, clock=frozen(T1))
    second = start_session(project, clock=frozen(T0))  # same microsecond
    assert second == first + "-2"
    close_session(project, second, clock=frozen(T1))
    third = start_session(project, clock=frozen(T0))
    assert third == first + "-3"


# --- close_session ---------------------------------------------------------------


def test_close_records_end(project):
    session_id = start_session(project, clock=frozen(T0))
    closed = close_session(project, session_id, clock=frozen(T1))
    assert closed == Session(
        id=session_id,
        started_at="2026-11-20T22:00:00.000+00:00",
        ended_at="2026-11-20T23:30:00.000+00:00",
    )
    s = list_sessions(project)[0]
    assert s.ended_at == "2026-11-20T23:30:00.000+00:00"
    assert get_open_session(project) is None
    row = project.db.execute(
        "SELECT ended_at FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    assert row == ("2026-11-20T23:30:00.000+00:00",)


def test_close_unknown_session_raises(project):
    with pytest.raises(SessionNotFoundError) as excinfo:
        close_session(project, "no-such-session")
    assert "no-such-session" in str(excinfo.value)
    assert issubclass(SessionNotFoundError, ProjectError)


def test_close_twice_raises(project):
    session_id = start_session(project, clock=frozen(T0))
    close_session(project, session_id, clock=frozen(T1))
    with pytest.raises(SessionAlreadyClosedError) as excinfo:
        close_session(project, session_id, clock=frozen(T1 + timedelta(hours=1)))
    assert session_id in str(excinfo.value)
    assert "2026-11-20T23:30:00.000+00:00" in str(excinfo.value)  # names the existing end
    assert issubclass(SessionAlreadyClosedError, ProjectError)
    # the original end is unchanged
    assert list_sessions(project)[0].ended_at == "2026-11-20T23:30:00.000+00:00"


def test_close_default_clock_is_utc_now(project):
    session_id = start_session(project)
    closed = close_session(project, session_id)
    parsed = datetime.fromisoformat(closed.ended_at)
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)
    assert parsed <= datetime.now(timezone.utc) + timedelta(seconds=5)


# --- list_sessions / get_open_session ----------------------------------------------


def test_list_sessions_ordered_by_started_at(project):
    # create out of chronological order via explicit started_at
    late = start_session(project, started_at=datetime(2026, 11, 22, 21, 0, tzinfo=timezone.utc))
    close_session(project, late, ended_at="2026-11-22T23:00:00+00:00")
    early = start_session(project, started_at=datetime(2026, 11, 20, 22, 0, tzinfo=timezone.utc))
    close_session(project, early, ended_at="2026-11-20T23:00:00+00:00")
    mid = start_session(project, started_at="2026-11-21T22:30:00+00:00")
    close_session(project, mid, ended_at="2026-11-21T23:45:00Z")

    sessions = list_sessions(project)
    assert [s.id for s in sessions] == [early, mid, late]
    assert [s.started_at for s in sessions] == [
        "2026-11-20T22:00:00.000+00:00",
        "2026-11-21T22:30:00.000+00:00",
        "2026-11-22T21:00:00.000+00:00",
    ]
    assert all(s.ended_at is not None for s in sessions)


def test_list_sessions_empty(project):
    assert list_sessions(project) == []


def test_get_open_session_none_when_all_closed(project):
    assert get_open_session(project) is None
    session_id = start_session(project, clock=frozen(T0))
    open_s = get_open_session(project)
    assert open_s == Session(id=session_id, started_at="2026-11-20T22:00:00.000+00:00", ended_at=None)
    close_session(project, session_id, clock=frozen(T1))
    assert get_open_session(project) is None


# --- started_at / ended_at argument forms -------------------------------------------


def test_start_accepts_aware_datetime_in_other_timezone(project):
    # 2026-11-21 00:00:00 at +02:00 == 2026-11-20 22:00:00 UTC
    plus2 = timezone(timedelta(hours=2))
    session_id = start_session(project, started_at=datetime(2026, 11, 21, 0, 0, tzinfo=plus2))
    s = list_sessions(project)[0]
    assert s.started_at == "2026-11-20T22:00:00.000+00:00"
    assert session_id == "20261120T220000000000Z"


def test_start_accepts_naive_datetime_as_utc(project):
    """Documented choice: a naive datetime is treated as UTC."""
    session_id = start_session(project, started_at=datetime(2026, 11, 20, 22, 0, 0))
    assert list_sessions(project)[0].started_at == "2026-11-20T22:00:00.000+00:00"
    assert session_id == "20261120T220000000000Z"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-11-20T22:00:00+00:00", "2026-11-20T22:00:00.000+00:00"),
        ("2026-11-20T22:00:00Z", "2026-11-20T22:00:00.000+00:00"),
        ("2026-11-20T22:00:00.5+00:00", "2026-11-20T22:00:00.500+00:00"),
        ("2026-11-21T00:00:00+02:00", "2026-11-20T22:00:00.000+00:00"),
    ],
)
def test_start_accepts_iso_strings(project, value, expected):
    start_session(project, started_at=value)
    assert list_sessions(project)[0].started_at == expected


def test_close_accepts_iso_string_and_datetime(project):
    session_id = start_session(project, clock=frozen(T0))
    close_session(project, session_id, ended_at="2026-11-20T23:30:00Z")
    assert list_sessions(project)[0].ended_at == "2026-11-20T23:30:00.000+00:00"
    other = start_session(project, clock=frozen(T1))
    close_session(project, other, ended_at=T1 + timedelta(hours=1))
    assert list_sessions(project)[1].ended_at == "2026-11-21T00:30:00.000+00:00"


@pytest.mark.parametrize("bad", ["not a date", "2026-13-40T99:00:00", ""])
def test_start_rejects_unparseable_iso_string(project, bad):
    with pytest.raises(ValueError, match="ISO-8601"):
        start_session(project, started_at=bad)
    assert list_sessions(project) == []


@pytest.mark.parametrize("bad", [42, 3.14, ["2026-11-20T22:00:00Z"], object()])
def test_start_rejects_wrong_type(project, bad):
    with pytest.raises(TypeError):
        start_session(project, started_at=bad)
    assert list_sessions(project) == []


def test_close_rejects_unparseable_iso_string(project):
    session_id = start_session(project, clock=frozen(T0))
    with pytest.raises(ValueError, match="ISO-8601"):
        close_session(project, session_id, ended_at="nope")
    # the session is still open (transaction rolled back)
    assert get_open_session(project).id == session_id


# --- persistence across reopen --------------------------------------------------------


def test_sessions_persist_across_reopen(root, project):
    first = start_session(project, clock=frozen(T0))
    close_session(project, first, clock=frozen(T1))
    second = start_session(project, clock=frozen(T1 + timedelta(hours=2)))
    project.close()

    with open_project(root / "m-42") as p:
        sessions = list_sessions(p)
        assert [s.id for s in sessions] == [first, second]
        assert sessions[0].ended_at == "2026-11-20T23:30:00.000+00:00"
        assert sessions[1].ended_at is None
        open_s = get_open_session(p)
        assert open_s is not None and open_s.id == second
        # the reopened project enforces the policy too
        with pytest.raises(SessionOpenError):
            start_session(p)
        close_session(p, second, ended_at="2026-11-21T02:00:00+00:00")
        assert get_open_session(p) is None


# --- public API ------------------------------------------------------------------------


def test_session_api_is_exported_from_package():
    import seestar_controller.store as store

    for name in (
        "Session",
        "SessionAlreadyClosedError",
        "SessionNotFoundError",
        "SessionOpenError",
        "start_session",
        "close_session",
        "list_sessions",
        "get_open_session",
    ):
        assert name in store.__all__, name
        assert hasattr(store, name), name
    assert issubclass(store.SessionOpenError, store.ProjectError)
    assert issubclass(store.SessionNotFoundError, store.ProjectError)
    assert issubclass(store.SessionAlreadyClosedError, store.ProjectError)
    assert callable(store.start_session)
    assert callable(store.close_session)
    assert callable(store.list_sessions)
    assert callable(store.get_open_session)
    assert isinstance(store.Session, type)


def test_sessions_module_imports_no_gtk_and_no_link():
    import subprocess
    import sys

    code = (
        "import sys;"
        "import seestar_controller.store.sessions;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert not any(m == 'seestar_controller.link' or m.startswith("
        "'seestar_controller.link.') for m in sys.modules), 'link imported';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout
