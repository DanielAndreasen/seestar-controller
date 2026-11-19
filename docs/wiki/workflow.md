# Workflow

- One spec at a time, one issue at a time, in dependency order (blockers first).
- Each issue: spawn a fresh **implementor** subagent → wait for its report → commit its work (ticket's own commit) → spawn a **review** subagent → fix findings if any (same ticket commit or follow-up commit on the same ticket) → next issue.
- Subagents get self-contained instructions: repo path, ticket path, conventions (AGENTS.md, GLOSSARY.md, uv, pytest), and must run tests before reporting.
- Communication with subagents via context pointers (ticket files, commits, wiki), not duplicated prose.
- Wiki is the memory across sessions: ingest durable learnings, date every claim, cite commits.
