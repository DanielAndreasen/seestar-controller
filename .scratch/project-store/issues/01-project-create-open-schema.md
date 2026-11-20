# 01: Project create/open + schema

**What to build:** the project store foundation — create a project directory under the project root (default `~/SeestarProjects/<slug>/`) with `project.json` and a SQLite database initialized with the schema (sessions, frames, stars, measurements); open an existing project and list all projects. Demoable: create a project in a temp root, reopen it, list it.

**Blocked by:** seestar-link 01 (uv project scaffold)

**Status:** implemented in 429315c

- [x] `create_project(name, root)` makes the directory layout and empty db
- [x] `open_project(path)` loads an existing project, validates layout
- [x] `list_projects(root)` returns all projects
- [x] Schema covers sessions, frames, stars (role target/comp, pixel + RA/Dec), measurements (frame FK, jd, mag, mag_err, excluded flag)
- [x] Tests on a temp dir: create → reopen → list round-trip

## Comments

Implemented as the new pure-Python package `src/seestar_controller/store/` (stdlib only, no GTK / no link imports — subprocess-enforced in tests): `errors.py` (`ProjectError` base + `ProjectExistsError` / `ProjectNotFoundError` / `InvalidProjectError`), `schema.py` (`SCHEMA_VERSION = 1`, DDL for sessions/frames/stars/measurements with PKs, FKs `ON DELETE CASCADE`, CHECKs on `kind`/`role`/`excluded`, indexes, `PRAGMA user_version`, initialized in ONE transaction with `PRAGMA journal_mode=WAL`), `project.py` (`slugify`, `create_project`, `open_project`, `list_projects`, `Project` with `.path`/`.name`/`.slug`/`.config`/`.db` + atomic `save_config()`, `ProjectSummary`, root resolution: explicit arg > `SEESTAR_PROJECT_ROOT` env var > `~/SeestarProjects/`). Layout: `<root>/<slug>/{project.json, db.sqlite, subs/, stacks/}`; session subdirs stay lazy for ticket 03. `project.json` at creation: `schema_version`, `name`, `slug`, `created_at` (UTC ISO-8601 with `+00:00`), and `null` placeholders `target` / `star_picks` / `period`. Documented choices: (1) slug collisions raise `ProjectExistsError` instead of suffixing (two names slugging alike would differ only invisibly); (2) `open_project` requires db `user_version` == config `schema_version` and all schema tables present, else `InvalidProjectError`; (3) `list_projects` skips stray/unparseable dirs silently and sorts by `created_at`, then slug. Tests: `tests/test_store_project.py` (68 tests, tmp_path only, no network): create→reopen→list round-trip, project.json contents + schema_version, PRAGMA table_info/index_list/foreign_key_list, WAL mode (incl. persisted across reopen), user_version, CHECK + FK enforcement, transaction rollback, slugify cases + collision, all open-error paths, create-existing → `ProjectExistsError`, list empty/multiple/skips, atomic `save_config` (no `*.tmp` leftovers; failing `os.replace` / `KeyboardInterrupt` at `fsync` keep the original), env-var root precedence, module-boundary subprocess test, public-API export test.
