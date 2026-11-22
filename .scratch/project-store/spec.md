# Spec: project-store

Status: completed 2026-11-20 — all 7 issues implemented and reviewed

## Problem Statement

Observing campaigns span many nights. All data for one target — sessions, sub-frames, stacks, star selections, measurements — must persist in one project the user can reopen, extend, and export.

## Solution

A project store: each project is a directory under `~/SeestarProjects/<name>/` (configurable root) containing `project.json` (settings, star selections, found period), a SQLite database (sessions, frames, measurements), `subs/` and `stacks/` for FITS files, and `lightcurve.csv` as the export artifact.

## User Stories

1. As an observer, I want to create a project for a target, so that all its data lives in one place.
2. As an observer, I want to reopen a project on a later night, so that the campaign continues where it left off.
3. As an observer, I want each night recorded as a session with start/end time, so that the lightcurve can be split by night.
4. As an observer, I want sub-frames stored with metadata (time, exposure, gain, target), so that photometry knows what each frame is.
5. As an observer, I want stacks stored alongside subs, so that enhancement works on them.
6. As an observer, I want my target/comp star selections saved in the project, so that I don't re-pick them every night.
7. As an observer, I want measurements appended to the project's lightcurve, so that nights accumulate.
8. As an observer, I want to export the lightcurve as CSV, so that I can use it in other tools.
9. As an observer, I want to list and delete old projects, so that I manage disk usage.
10. As an observer, I want the store to be robust against crashes mid-write, so that a power cut doesn't corrupt the project.

## Implementation Decisions

- Layout: `~/SeestarProjects/<slug>/` with `project.json`, `db.sqlite`, `subs/<session>/<frame>.fits`, `stacks/<session>/<stack>.fits`, `lightcurve.csv`.
- SQLite tables: sessions, frames, stars (role: target/comp, x/y or RA/Dec), measurements (frame FK, jd, mag, mag_err).
- Times stored as JD (and UTC ISO) — JD is the photometry standard.
- `project.json` holds target name/coords, star picks, aperture settings, found period, enhance presets.
- Writes: SQLite transactions; FITS written to temp then renamed (atomic).
- Module boundary: pure Python, no GTK; async-friendly API for the UI.
- CSV export columns: `jd, utc, mag, mag_err, session, target`.
- Story 9 (list + delete old projects): **implemented** (app-improvement ticket 16) — `store.delete_project(path)` removes the whole project directory via `shutil.rmtree`, but only after validating the directory really is a project (parseable `project.json` with the required keys, else `ProjectNotFoundError`/`InvalidProjectError` and nothing is touched) and only when it is not open (every `open_project` handle registers in a weak set until `close()`; while any live handle has the path, `ProjectOpenError` is raised). The Projects view offers a per-row Delete button; `ProjectController.delete_project` closes the current project first when it is the one being deleted and clears `last_project`.

## Testing Decisions

- Good test = round-trip behavior on a temp dir: create project, add session/frames/measurements, reopen, query lightcurve across sessions, export CSV and re-parse.
- Schema migration test when schema evolves; crash-safety test (kill mid-write, reopen).

## Out of Scope

- Cloud sync, multi-user access, locking.
- Versioning of project files beyond atomic writes.

## Further Notes

- Project root configurable in settings (default `~/SeestarProjects/`).
