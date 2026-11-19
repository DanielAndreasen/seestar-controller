# 05: Headless controller test harness

**What to build:** the test infrastructure that keeps UI logic verifiable without a display — controllers separated from widgets, a harness that runs controller tests headless, and a smoke test that instantiates every view with a dummy GTK backend. Demoable: `uv run pytest` passes with no display.

**Blocked by:** 01

**Status:** ready-for-agent

- [ ] Controller layer holds app logic, widgets stay thin
- [ ] Controller tests run headless (no display needed)
- [ ] Smoke test constructs every view headless
- [ ] CI-style command documented: `uv run pytest`
