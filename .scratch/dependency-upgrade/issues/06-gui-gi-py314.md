# 06: Make the GUI launch again on Python 3.14 (gi/PyGObject for 3.14)

**What to build:** since the 3.12 -> 3.14 bump, `uv run seestar-controller gui` fails with `No module named 'gi'`: the system PyGObject is built for CPython 3.12 only (`_gi.cpython-312-...so`), so it cannot load in the 3.14 project venv even with `--system-site-packages` (docs/wiki/assumptions.md, 2026-11-21 entry). Find the SIMPLEST way to get a working `gi` for the 3.14 interpreter on the user's desktop and implement it: candidate options, cheapest first — (a) system `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1` packages built for a 3.14 system python + `uv venv --system-site-packages --clear .venv && uv sync`; (b) a pip-installable PyGObject wheel for cp314 if one exists (check PyPI; if it exists, adding it as a dependency is acceptable ONLY for the GUI path and must not break headless installs — e.g. optional extra, never a hard dep, and `uv add pygobject` as a hard dependency stays forbidden); (c) building PyGObject from source against 3.14 (needs libgirepository-2.0-dev etc.) — only if (a)/(b) fail. Whichever is chosen: document the exact steps in docs/wiki (dated entry) and in the CLI's clean-error message, keep the pure/widget split and `pytest.importorskip("gi")` behavior intact, keep headless installs working (no hard gi dependency). Demoable: on the desktop, `uv run seestar-controller gui` opens the window under Python 3.14; headless machines still get the clean error; full suite green (widget tests run when gi present, skip otherwise); ty + ruff clean.

**Blocked by:** dependency-upgrade 01, 03, 06 is independent of 04/05

**Status:** ready-for-agent

- [ ] Working `gi` import under the 3.14 venv on the desktop via the simplest option; exact steps documented (dated wiki entry + CLI error message updated)
- [ ] No hard PyGObject dependency; headless install/test path unchanged
- [ ] Full suite green; `uv tool run ty check` + `uv tool run ruff check` clean; GUI launches on 3.14

## Comments

(implementor fills in)
