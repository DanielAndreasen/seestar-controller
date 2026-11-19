# 01: Project create/open + schema

**What to build:** the project store foundation — create a project directory under the project root (default `~/SeestarProjects/<slug>/`) with `project.json` and a SQLite database initialized with the schema (sessions, frames, stars, measurements); open an existing project and list all projects. Demoable: create a project in a temp root, reopen it, list it.

**Blocked by:** seestar-link 01 (uv project scaffold)

**Status:** ready-for-agent

- [ ] `create_project(name, root)` makes the directory layout and empty db
- [ ] `open_project(path)` loads an existing project, validates layout
- [ ] `list_projects(root)` returns all projects
- [ ] Schema covers sessions, frames, stars (role target/comp, pixel + RA/Dec), measurements (frame FK, jd, mag, mag_err, excluded flag)
- [ ] Tests on a temp dir: create → reopen → list round-trip
