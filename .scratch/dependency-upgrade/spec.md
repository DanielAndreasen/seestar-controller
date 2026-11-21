# Spec: dependency-upgrade

Status: completed 2026-11-21 (01: 3c07d0f; 02: 9a3fa47; 03: 3af6e31; 04: 157a1a0; 05: ff3370f; 06: 2241c6b+f622ba1)

## Problem Statement

The project pins its dependencies and Python version conservatively. The user wants the software upgraded as far as possible — latest Python, latest library versions — without breaking any existing feature or test.

## Solution

A systematic dependency upgrade: bump the required Python version to the latest stable supported by all dependencies, upgrade every library in `pyproject.toml`/`uv.lock` to its latest compatible release, fix any breakage from API changes, and keep the full test suite green (modulo the known pre-existing headless/gi environment failures). Each upgrade step must be verifiable: tests + `ty check` + `ruff check` green after each bump.

## User Stories

1. As a developer, I want the project on the latest stable Python version, so that I get performance and language improvements.
2. As a developer, I want all libraries at their latest compatible versions, so that I get bug fixes and security updates.
3. As a developer, I want the upgrade done without breaking any feature, so that all existing tests still pass.
4. As a developer, I want breakages from API changes fixed properly (not worked around), so that the code stays clean.
5. As a developer, I want `uv.lock` regenerated and committed, so that installs are reproducible.
6. As a developer, I want `ty check` and `ruff check` green after the upgrade, so that lint/type standards hold.
7. As a developer, I want a record of what was upgraded and why, so that future upgrades are easier.

## Implementation Decisions

- Use `uv` for everything (`uv lock --upgrade`, `uv sync`); never `uv add pygobject` (system GTK bindings stay system-provided).
- Upgrade in small verifiable steps (Python version first, then dependency groups), committing each green step.
- Known pre-existing failures in gi/headless sandboxes (test_observing_widgets x3, test_report_widgets x3, test_report_sections movers x1, test_moving_objects_widgets x5, test_visibility_planner purity) are NOT caused by upgrades — verify any new failure against main before fixing.
- Record the upgrade in docs/wiki (dated entry) with before/after versions.

## Testing Decisions

- Good test = full suite `uv run pytest -q` green (modulo known pre-existing env failures) after each step, plus `uv tool run ty check` and `uv tool run ruff check` clean.
- Prior art: the lint-ty-ruff ticket workflow.

## Out of Scope

- New features; this spec is upgrade-only.
- Upgrading the system GTK/PyGObject itself (system package, not pip).
- Changing behavior to accommodate new library defaults (features must not change).

## Further Notes

- Check each library's changelog for breaking changes between current and latest (numpy 2.x, scipy, astropy, matplotlib, reportlab, Pillow, etc.).
- Python version bump may require `requires-python` change in pyproject.toml and CI/env notes in AGENTS.md if present.

## Open Questions

- How far to go: latest stable of everything, or LTS where a library offers both? Propose: latest stable of everything, fall back only on hard conflicts.
