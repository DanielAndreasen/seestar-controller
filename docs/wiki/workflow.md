# Workflow

- One spec at a time, one issue at a time, in dependency order (blockers first).
- Each issue: spawn a fresh **implementor** subagent → wait for its report → commit its work (ticket's own commit) → spawn a **review** subagent → fix findings if any (same ticket commit or follow-up commit on the same ticket) → next issue.
- Subagents get self-contained instructions: repo path, ticket path, conventions (AGENTS.md, GLOSSARY.md, uv, pytest), and must run tests before reporting.
- Communication with subagents via context pointers (ticket files, commits, wiki), not duplicated prose.
- Wiki is the memory across sessions: ingest durable learnings, date every claim, cite commits.

## Testing (CI-style)

- `uv sync && uv run pytest` runs the full suite headless: pure tests pass with
  no display; widget tests (`tests/test_ui_widgets.py`) skip without GTK
  (`pytest.importorskip("gi")`; when `gi` is importable but there is no
  display, `Gtk.init_check()` returns False and they skip too). On the desktop
  with `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1` they run — optionally under
  `xvfb-run -a uv run pytest` (a virtual display standing in for a dummy GTK
  backend).
- ui pure/widget split rule (enforced by a subprocess test in
  `tests/test_ui_harness.py`): pure ui modules (`state`, `settings`,
  `connection`, `notifications`) never import `gi`; widget modules import `gi`
  at module top and are lazy-exported from `ui/__init__.py` (PEP 562), so
  `import seestar_controller.ui` never imports GTK. Controller tests build
  the app-level controllers headless via the `ui_harness` fixture in
  `tests/conftest.py` (fakes only — no network, no gi).
