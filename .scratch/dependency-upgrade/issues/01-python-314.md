# 01: Bump Python 3.12 -> 3.14

**What to build:** move the project from Python 3.12 to the latest stable available locally (3.14.7): change `requires-python` in pyproject.toml to `>=3.14,<3.15`, regenerate `uv.lock` (`uv lock`), `uv sync`, fix any 3.14 incompatibilities in code/tests (e.g. removed stdlib modules, changed defaults), keep the full suite green and `uv tool run ty check` / `uv tool run ruff check` clean. Demoable: `uv run python --version` prints 3.14.x, full test suite passes, GUI still launches.

**Blocked by:** None (can start immediately)

**Status:** implemented in 3c07d0f (merged)

- [ ] `requires-python = ">=3.14,<3.15"` in pyproject.toml; `uv lock` + `uv sync` green
- [ ] Any code/test fixes needed for 3.14 (no behavior change)
- [ ] Full suite `uv run pytest -q` green (known pre-existing headless/gi failures excepted — verify against main), ty + ruff clean
- [ ] GUI launches: `uv run seestar-controller gui` starts
- [ ] Commit lock + changes as one ticket commit

## Comments

(implementor fills in)
