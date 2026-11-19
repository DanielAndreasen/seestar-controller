# 04: Session logging + frame downloads

**What to build:** data capture during the night — sub-frames and stacks downloaded into the current session as they arrive, plus a per-session event log (goto, stack start/stop, errors) viewable in the app. Demoable: mock scope produces frames during a run, they land in the project session and appear in the log.

**Blocked by:** 02, seestar-link 06, project-store 03

**Status:** ready-for-agent

- [ ] Frames arriving during stacking are downloaded into the session directory
- [ ] Each stored frame has its metadata row in the project db
- [ ] Session event log records goto/stack/error events with timestamps
- [ ] Log viewable per session in the UI
- [ ] Tests: mock run → frames + log present in temp project
