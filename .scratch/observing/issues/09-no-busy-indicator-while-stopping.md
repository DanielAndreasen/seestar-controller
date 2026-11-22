# 09: No busy/progress indication while the night stops and final frames download

**Found during:** live guided test (project ngc869, 2026-11-22). Pressing **Stop** froze the app window for ~1 minute with no feedback; when it recovered, the final `Stacked_….fit` had landed in `stacks/<session>/`. The stop path (executor stop -> scope stop_stacking -> capture downloads the final stack frame -> session closed) runs without any UI progress indication, so the window looks hung.

**What to build:** the Observe view must show a busy/progress state while a stop is in flight — e.g. disable Stop/Park/Start, show "Stopping… downloading final frames…" in the status row (or a spinner), until the executor reaches `done` and the capture has stored the final frames. Acceptance: click Stop -> controls insensitized + status text shown immediately -> when `run done` + final frame stored, controls re-enable; no unresponsive window. Tests: widget-level — stop in flight shows busy state; completion clears it.

**Blocked by:** None (observing 01-05 done; relates to 07 live refresh)

**Status:** implemented in cff007b (pending review)

- [ ] Observe view busy state while executor stop + final frame download is in flight
- [ ] Status text ("Stopping…") in the observe view
- [ ] Tests: busy state set on stop, cleared on done; buttons gated
- [ ] Full suite green
