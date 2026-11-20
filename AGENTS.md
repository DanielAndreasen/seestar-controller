# AGENTS.md

## Ticket commits

Each ticket gets its own git commit (or commits if appropriate). Work on a ticket in its own commit(s); never bundle multiple tickets into one commit. Local repo only, no remote.

## Branches

New features are developed on a new branch off `main`, not on `main` itself. Merge (or fast-forward) into `main` only when the ticket is complete and reviewed.

## Lint/typecheck (last implementation step)

Before committing a ticket's code, run `uv run ty check` and `uv run ruff check` and fix all errors and warnings they report.

## Agent skills

### Issue tracker

Issues live as local markdown under `.scratch/<feature-slug>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` + `docs/adr/` at repo root. See `docs/agents/domain.md`.
