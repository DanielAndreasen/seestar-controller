# 17: "implemented (merged)" issue files still show every acceptance checkbox unchecked

**What to build:** tick the acceptance checkboxes (and fill the "(implementor fills in)" comments) on issue files whose Status line already says implemented and merged, so the files reflect reality.

Evidence: `.scratch/session-report/issues/02-quality-movers-sections.md`, `.scratch/sub-frames/issues/01-viewer-opens-subs.md`, `02-solve-crossmatch-subs.md`, `03-stack-subs.md`, `.scratch/transit-fit/issues/01-fit-core.md`, `.scratch/variable-scan/issues/01-scan-core.md`, `02-candidate-view.md`, `.scratch/observing/issues/05-stale-session-dialog.md`, `.scratch/photometry/issues/05-picking-real-seestar-frames.md`, `.scratch/offline-analysis/issues/01-offline-mode-core.md`, `02-offline-analysis-views.md`, `03-recent-projects.md` — all show "Status: implemented in <hash> (merged)" yet `- [ ]` on every acceptance line. The code demonstrably exists and the matching tests pass (verified by the audit: e.g. 527 passed across the targeted files).

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] Every listed issue file has its acceptance boxes checked (or a pointer to the implementing commit's test list) matching its Status line
- [ ] No code changes; docs-only ticket

## Comments

Audit finding (kind: incorrect-spec-claim, severity: low; systemic doc-hygiene pattern).
