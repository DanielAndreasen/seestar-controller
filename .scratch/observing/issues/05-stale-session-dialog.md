# 05: Handle stale open session on Start night (resume/close offer)

**What to build:** when "Start night" raises `SessionOpenError` because a previous session of the project is still marked open (e.g. after the app was killed), the Observe view must not just show an error — it must offer a dialog: "Session <id> from <time> is still open. Resume it or close it and start a new one?" with Resume / Close-and-start-new buttons. Closing calls `store.sessions.close_session`; resuming reattaches the executor to the open session. Demoable: open a project with a stale open session, press Start night -> dialog appears; both choices work; no more raw SessionOpenError.

**Blocked by:** None (observing 01-04, project-store done)

**Status:** ready-for-agent

- [ ] Observe view / observe_controller catches SessionOpenError on start and shows the resume/close dialog (GLib main loop safe)
- [ ] Resume path reattaches to the open session; close path calls close_session then starts fresh
- [ ] Tests: stale open session -> dialog shown (widget harness), both paths; no SessionOpenError surfaces raw
- [ ] Full suite green

## Comments

(implementor fills in)
