# 18: Stale spec status lines and runtime claims (observing 05, photometry 05, ui-shell Python 3.12)

**What to build:** correct spec header lines that contradict merged commits and the actual runtime.

Evidence:
- `.scratch/observing/spec.md:3` says "05 ready-for-agent" but issue 05 is implemented and merged (commits 9533c092 + d4beeb9; `StaleSessionDialog` wired in `observing/observe_view.py:449-510` and `observe_controller.py:651-714`).
- `.scratch/photometry/spec.md:3` says "05 ready-for-agent (picking fails on real Seestar frames: no WCS subs, RGB stacks)" but 05 is merged (commit 8f00932); the parenthetical describes a bug that no longer exists (`photometry/picking.py:45-54` handles missing WCS pixel-only picks and RGB-stack WCS subsets; ticket-05 tests pass).
- `.scratch/ui-shell/spec.md:26` says "Python 3.12" but the project is Python 3.14-only (`pyproject.toml:6` `requires-python = ">=3.14,<3.15"`, README/AGENTS.md "Python 3.14").

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] The three spec lines above are updated to match the merged reality
- [ ] No code changes; docs-only ticket

## Comments

Audit finding (kind: incorrect-spec-claim, severity: low).
