# AGENTS.md

## Ticket commits

Each ticket gets its own git commit (or commits if appropriate). Work on a ticket in its own commit(s); never bundle multiple tickets into one commit. Local repo only, no remote.

## Branches

New features are developed on a new branch off `main`, not on `main` itself. Merge (or fast-forward) into `main` only when the ticket is complete and reviewed.

## Worktrees

New work (tickets, bug fixes) is done in its own git worktree under `$VIBE_HOME/worktrees/` (default `~/.vibe/worktrees/`, per-repo bucket), on a new branch off `main` — not as a branch in the main checkout. The main agent merges finished, reviewed work into `main` and resolves any merge conflicts itself; if a conflict is difficult, it may ask the implementing subagent for help. Worktrees are cleaned up after their branch is merged.

## Lint/typecheck (last implementation step)

Before committing a ticket's code, run `uv tool run ty check` and `uv tool run ruff check` and fix all errors and warnings they report.

## Agent skills

### Issue tracker

Issues live as local markdown under `.scratch/<feature-slug>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` + `docs/adr/` at repo root. See `docs/agents/domain.md`.
