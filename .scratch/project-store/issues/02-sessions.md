# 02: Sessions

**What to build:** session lifecycle — start a session (night) in a project with UTC start time, close it with end time, list sessions. Demoable: open project, start session, close it, session appears in listing with both timestamps.

**Blocked by:** 01

**Status:** implemented in c07b9e4

- [x] `start_session(project)` records UTC start, returns session id
- [x] `close_session(project, session_id)` records UTC end
- [x] Sessions listed per project in order
- [x] Starting a session while one is open is rejected or auto-closed (pick one, document it)
- [x] Tests: start/close/list on temp project

## Comments

Implemented in `src/seestar_controller/store/sessions.py` (pure Python, stdlib only, no GTK / no link imports — subprocess-enforced in tests), errors added to `errors.py`, all exported from `store/__init__.py`. API: `start_session(project, started_at=None, *, clock=...) -> str` (session id), `close_session(project, session_id, ended_at=None, *, clock=...) -> Session`, `list_sessions(project) -> list[Session]` ordered by `started_at` (ties by id), `get_open_session(project) -> Session | None`; `Session` is a frozen dataclass `(id, started_at, ended_at)`. Documented choices: (1) session id = UTC ISO-8601 basic-format timestamp slug `YYYYMMDDTHHMMSSffffffZ` (e.g. `20261120T220000123456Z`) — lexicographic == chronological, filesystem-safe (doubles as the `subs/<session>/` dir name of ticket 03), unique to the microsecond, with a deterministic `-2`/`-3`… suffix on collision (only possible with a frozen clock); (2) starting while a session is open is REJECTED with `SessionOpenError` naming the open session — auto-close was the alternative, rejection is safer for data integrity (a session left open by a crash is never silently ended); (3) close of an unknown id raises `SessionNotFoundError`, close of an already-closed session raises `SessionAlreadyClosedError`; (4) times stored as UTC ISO-8601 strings with `+00:00` and millisecond precision (same format as `utc_now_iso`); `started_at`/`ended_at` accept datetime (naive = UTC, aware converted), ISO string (`Z` ok), or None = now from the injectable `clock` (default system UTC clock); (5) all writes in one transaction (`with project.db:`), so rejected start/close leave no partial row. Tests: `tests/test_store_sessions.py` (31 tests, tmp_path only, no network): start→list with start/no end + raw NULL row, timestamp-slug id incl. microseconds, default clock is UTC now, start-while-open rejected (message names the open id, nothing written), start-after-close ok, id-collision suffix, close records end + returns Session, close unknown / close twice errors (original end unchanged), list ordered by started_at (created out of order), list empty, get_open_session None/open/None, aware/non-UTC/naive datetime + ISO string forms, unparseable string → ValueError, wrong type → TypeError, close with bad string rolls back (still open), sessions persist across reopen (incl. policy enforced on the reopened project), public-API export test, module-boundary subprocess test.
