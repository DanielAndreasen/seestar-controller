# 02: Executor state machine (headless)

**What to build:** the night executor as a headless-testable state machine — idle → resolving → slewing → stacking → next | done — driving the device facade through the queue, auto-advancing, opening/closing the project session. Demoable: run a 2-target queue against the mock scope with no UI and verify both were visited and the session logged.

**Blocked by:** seestar-link 04, project-store 02

**Status:** ready-for-agent

- [ ] Executor walks the queue via the facade: goto → start stack → done → next
- [ ] State transitions are explicit and observable (for the UI to render)
- [ ] Session opened at start, closed at end in project-store
- [ ] Scope error mid-queue → defined policy (skip vs abort), documented
- [ ] Tests: full queue run against mock link; error path; pause/skip/stop
