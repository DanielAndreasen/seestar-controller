# 03: Observe view wiring

**What to build:** the Observe view end to end — queue editor on one side, live preview stream and queue progress (current target, frames stacked, elapsed) on the other, with pause / skip / stop / park controls bound to the executor. Demoable: against the mock scope, start the night and watch preview + progress update.

**Blocked by:** 01, 02, seestar-link 05

**Status:** ready-for-agent

- [ ] Live preview frames render in the Observe view
- [ ] Progress shows current target, stack count, elapsed
- [ ] Pause / skip / stop / park buttons drive the executor
- [ ] View renders executor state changes without freezing
- [ ] Tests: view controller with mock executor + mock stream
