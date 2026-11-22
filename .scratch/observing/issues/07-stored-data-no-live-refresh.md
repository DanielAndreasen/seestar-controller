# 07: Stored-data browser does not refresh when frames are stored mid-session

**Found during:** live guided test (project ngc869, 2026-11-22). FITS subs landed on disk and in the project store during a running night, but the Projects view "Stored data" list kept showing the old counts until the project was re-opened. Root cause: `ProjectsView._on_project_event` (`src/seestar_controller/ui/views.py:1396`) is the only trigger for `_rebuild_data` (:1425), and it fires on project open/close events only — `SessionCapture` storing a frame emits no event the projects view listens to.

**What to build:** the stored-data browser must re-render (or incrementally append) when a frame is stored in the current project during a night. Wire the capture/store layer to a listener (e.g. extend the existing notification bus or a frame-stored callback from `SessionCapture._apply` / `store_frame`) that the app forwards to `ProjectsView._rebuild_data()` via `invoke_on_main`. Acceptance: with a night running, each stored sub appears in Stored data within a poll cycle without reopening the project; session subtitle counts (subs/stacks) update live.

**Blocked by:** None (observing 01-05 done)

**Status:** completed — reviewed (APPROVE), merged to main in b9fc15c

- [ ] Frame-stored event plumbed from capture/store to the UI (main-loop safe)
- [ ] ProjectsView re-renders stored data on that event (or appends the row)
- [ ] Tests: storing a frame during a session updates the browser without a project reopen
- [ ] Full suite green
