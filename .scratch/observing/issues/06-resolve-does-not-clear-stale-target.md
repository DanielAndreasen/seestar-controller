# 06: Resolve must clear the previous resolved target so Add cannot queue it

**Found during:** live indoor/outdoor guided test (project ngc869, 2026-11-22). Typing a new name, clicking **Resolve**, then quickly clicking **Add to queue** queued the *previous* target: the goto went to the old RA/Dec and the scope refused it (`scope_goto` code 270). Root cause: `QueueEditor._on_resolve_clicked` (`src/seestar_controller/observing/queue_editor.py:335-352`) only assigns `self._resolved` *after* the (slow) catalog lookup; `_on_add_clicked` (:354+) then reuses the stale `_resolved` when the name field matches it. The resolve is synchronous on the UI thread, so the button appears dead for seconds and users click Add in between.

**What to build:** clicking **Resolve** must immediately invalidate the previous resolution: set `self._resolved = None` (and show a "resolving <name>…" status / make Add insensitive until resolve completes) so **Add to queue** can never enqueue the stale target. Also consider making the resolve non-blocking (worker thread + `invoke_on_main`) so the UI does not freeze during the catalog lookup. Acceptance: resolve "NGC 689", then before it finishes type "NGC 869" and click Resolve then Add quickly — the queue must contain NGC 869 (RA ~2.33h Dec +57°), never NGC 689; and after a failed resolve, Add with a name must error ("resolve first") instead of using the old coord.

**Blocked by:** None (observing 01-05 done)

**Status:** completed — reviewed (APPROVE), merged to main in 0983a18

- [x] `_on_resolve_clicked` clears `_resolved` (and status) at click time, before the lookup
- [x] Add insensitive / "resolving…" state while a resolve is in flight; name-mismatch or missing `_resolved` -> inline error, nothing queued
- [ ] Optional: resolve off the UI thread
- [x] Tests: stale `_resolved` cannot be added after re-Resolve; quick Resolve->Add queues the new target; failed resolve -> Add errors
- [x] Full suite green

## Comments

Implemented in `942e9a0`. `QueueEditor._on_resolve_clicked` (`src/seestar_controller/observing/queue_editor.py`) now sets `_resolved = None`, `_resolving = True`, a dim "resolving <name>…" status, and `add_button.set_sensitive(False)` at the START of the click, before the catalog lookup; on success it stores `(name, coord)` and shows the coord, on failure the inline error — either way `finally` clears `_resolving` and re-enables Add. `_on_add_clicked` now requires a successful Resolve for the exact name in the field: in-flight resolve -> "still resolving …", missing/mismatched `_resolved` -> "resolve '<name>' first — no resolved coordinates for it"; both are inline errors and queue nothing (the old `add_by_name` unresolved fallback was removed from the widget; the pure controller API is unchanged). Tests in `tests/test_observing_widgets.py`: `test_add_by_name_requires_a_successful_resolve`, `test_resolve_button_shows_inline_error_for_unknown_name` (extended: failed resolve -> Add errors, nothing queued), `test_add_with_a_name_different_from_the_resolved_one_errors`, `test_quick_resolve_then_add_never_queues_the_stale_target` (a slow fake resolver asserts mid-lookup that `_resolved is None`, Add is insensitive, status says "resolving…", and an Add click emitted mid-lookup queues nothing; afterwards Add queues the NEW resolved target). Full suite: 2882 passed + 2 skipped (environmental: reproject missing, one gi-gated skip), `ruff check` clean, `ty check` 108 pre-existing gi-stub diagnostics only. Deviation: the optional worker-thread resolve was NOT done — the click-time invalidation + Add gate already make the stale target impossible to queue, and keeping the resolve synchronous avoids GTK thread marshalling in this fix.
