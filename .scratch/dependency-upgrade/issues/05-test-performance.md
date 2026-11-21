# 05: Speed up the test suite

**What to build:** reduce wall-clock time of `uv run pytest -q` (currently several minutes) without weakening what is tested. First measure: run with `--durations=20` and identify the slowest tests/modules. Then apply the cheapest wins, in order: (a) run independent tests in parallel with `pytest-xdist` (`-n auto`) if it is a pure win — verify no test depends on shared global state, no network, and the known gi/headless skips behave identically; make the parallel invocation the documented default (e.g. add a `test` script/alias or document `uv run pytest -q -n auto` in AGENTS.md) only if it is reliable, otherwise document it as optional; (b) fix genuinely slow tests (fixtures rebuilt per test, repeated FITS I/O, sleeps) by hoisting/scoping fixtures or using tmp_path properly; (c) mark genuinely slow integration-style tests so they can be deselected in a fast loop (`-m "not slow"`) while still running in the full suite. Do NOT reduce coverage, do NOT skip tests that pass on main, do NOT touch the known pre-existing failures. Demoable: before/after timing table (dated, with commit) in docs/wiki, full suite still green with the same pass/fail set as main.

**Blocked by:** dependency-upgrade 01, 02, 03

**Status:** implemented in ff3370f (merged)

- [ ] `--durations` analysis + before/after timing recorded in docs/wiki (dated, with commit)
- [ ] Parallel run (`-n auto`) verified reliable OR documented as optional with rationale; fast-loop marker strategy documented if used
- [ ] Full suite green with identical pass/fail set as main; `uv tool run ty check` + `uv tool run ruff check` clean

## Comments

(implementor fills in)
