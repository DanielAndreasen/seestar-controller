# AGENTS.md

## Ticket commits

Each ticket gets its own git commit (or commits if appropriate). Work on a ticket in its own commit(s); never bundle multiple tickets into one commit. Local repo only, no remote.

## Branches

New features are developed on a new branch off `main`, not on `main` itself. Merge (or fast-forward) into `main` only when the ticket is complete and reviewed.

## Worktrees

New work (tickets, bug fixes) is done in its own git worktree under `$VIBE_HOME/worktrees/` (default `~/.vibe/worktrees/`, per-repo bucket), on a new branch off `main` — not as a branch in the main checkout. The main agent merges finished, reviewed work into `main` and resolves any merge conflicts itself; if a conflict is difficult, it may ask the implementing subagent for help. When a branch is successfully merged into `main`, the main agent cleans up its worktree (`git worktree remove` + delete the branch) so no stale worktrees accumulate.

## Lint/typecheck (last implementation step)

Before committing a ticket's code, run `uv tool run ty check` and `uv tool run ruff check` and fix all errors and warnings they report.

## Running tests

- Default (full suite, parallel — pytest-xdist is in the dev group and verified reliable: identical pass/fail set across repeated `-n auto` runs, no shared global state, no network, gi-gated suites skip identically): `uv run pytest -q -n auto`
- Serial full suite (same tests, ~2.5 min): `uv run pytest -q`
- Fast loop (deselects only the 7 `@pytest.mark.slow` integration-style tests; the full suite always runs them): `uv run pytest -q -n auto -m "not slow"`
- Slowest-test report: `uv run pytest -q -n auto --durations=20`
- In headless sandboxes without gi, the widget suites skip via `pytest.importorskip("gi")` and two known pre-existing failures remain (see `docs/wiki/dependency-upgrade.md`); do not "fix" them by weakening tests. Test-only I/O shims (fsync no-op + `PRAGMA synchronous=OFF`) live in `tests/conftest.py` — see the issue-05 section of `docs/wiki/dependency-upgrade.md` for why they are safe.

## Agent skills

### Issue tracker

Issues live as local markdown under `.scratch/<feature-slug>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` + `docs/adr/` at repo root. See `docs/agents/domain.md`.
