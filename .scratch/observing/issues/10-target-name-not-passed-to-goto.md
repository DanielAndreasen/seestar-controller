# 10: Target name not passed to goto — frames stored as "Unknown"

**Found during:** live guided test (project ngc869, 2026-11-22). Observed NGC 869 (resolved in the queue), but every stored frame is named `Light_Unknown_…` / `Stacked_…_Unknown_…` and the frame rows show target "Unknown". Root cause: `NightExecutor` slews with `self._device.goto(entry.coord.ra_hours, entry.coord.dec_deg)` (`src/seestar_controller/observing/executor.py:839`) — the `name=` kwarg of `Device.goto` (`src/seestar_controller/link/device.py:493-500`) is never filled from the queue entry's name, so the scope's current target stays "Unknown" and the frame metadata/filenames inherit it.

**What to build:** pass the entry's name through: `device.goto(ra, dec, name=entry.name)` (fall back to the resolved name / RA-Dec string when the entry was added by coords). Acceptance: queue "NGC 869" -> start night -> stored subs/stacks carry target "NGC 869" in meta and filenames (`Light_NGC 869_…`), frame rows in Stored data show the target. Tests: executor goto passes the name (mock scope records `scope_goto_target` params); stored frame meta target == entry name.

**Blocked by:** None (observing 01-05 done)

**Status:** implemented in 0380ef8 (pending review)

- [x] Executor passes the queue entry name to `Device.goto(name=...)`
- [x] Frame meta/filenames carry the target name
- [x] Tests: goto params include name; stored frame target == "NGC 869"
- [x] Full suite green
