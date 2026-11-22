# 06: Resolve must clear the previous resolved target so Add cannot queue it

**Found during:** live indoor/outdoor guided test (project ngc869, 2026-11-22). Typing a new name, clicking **Resolve**, then quickly clicking **Add to queue** queued the *previous* target: the goto went to the old RA/Dec and the scope refused it (`scope_goto` code 270). Root cause: `QueueEditor._on_resolve_clicked` (`src/seestar_controller/observing/queue_editor.py:335-352`) only assigns `self._resolved` *after* the (slow) catalog lookup; `_on_add_clicked` (:354+) then reuses the stale `_resolved` when the name field matches it. The resolve is synchronous on the UI thread, so the button appears dead for seconds and users click Add in between.

**What to build:** clicking **Resolve** must immediately invalidate the previous resolution: set `self._resolved = None` (and show a "resolving <name>…" status / make Add insensitive until resolve completes) so **Add to queue** can never enqueue the stale target. Also consider making the resolve non-blocking (worker thread + `invoke_on_main`) so the UI does not freeze during the catalog lookup. Acceptance: resolve "NGC 689", then before it finishes type "NGC 869" and click Resolve then Add quickly — the queue must contain NGC 869 (RA ~2.33h Dec +57°), never NGC 689; and after a failed resolve, Add with a name must error ("resolve first") instead of using the old coord.

**Blocked by:** None (observing 01-05 done)

**Status:** open — found in live test 2026-11-22

- [ ] `_on_resolve_clicked` clears `_resolved` (and status) at click time, before the lookup
- [ ] Add insensitive / "resolving…" state while a resolve is in flight; name-mismatch or missing `_resolved` -> inline error, nothing queued
- [ ] Optional: resolve off the UI thread
- [ ] Tests: stale `_resolved` cannot be added after re-Resolve; quick Resolve->Add queues the new target; failed resolve -> Add errors
- [ ] Full suite green
