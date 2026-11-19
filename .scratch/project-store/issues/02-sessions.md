# 02: Sessions

**What to build:** session lifecycle — start a session (night) in a project with UTC start time, close it with end time, list sessions. Demoable: open project, start session, close it, session appears in listing with both timestamps.

**Blocked by:** 01

**Status:** ready-for-agent

- [ ] `start_session(project)` records UTC start, returns session id
- [ ] `close_session(project, session_id)` records UTC end
- [ ] Sessions listed per project in order
- [ ] Starting a session while one is open is rejected or auto-closed (pick one, document it)
- [ ] Tests: start/close/list on temp project
