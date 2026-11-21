# 04: Stale-session dialog is never forgotten — view relies solely on Adw.Dialog's "closed" signal

**What to build:** make the observe view clear its stale-session dialog deterministically when the user picks resume or start-new, instead of only via the animation-gated "closed" signal, so `stale_dialog` becomes `None` in every environment (headless/Xvfb included).

Evidence: `src/seestar_controller/observing/observe_view.py:490,495-497` connects `"closed"` → `_on_stale_dialog_closed`; `stale_session_dialog.py` button handlers only call `self.close()`. Live probe (libadwaita 1.5.0): `Adw.Dialog.close()` never emits "closed" and the dialog stays visible even with a running main loop, so the view leaks the closed dialog. Failing tests: `tests/test_observing_widgets.py::test_stale_session_dialog_resume_button_resumes_the_session` and `::test_stale_session_dialog_close_button_closes_and_starts_new` (both `assert widget.stale_dialog is None`). The controller actions themselves work — only the dialog bookkeeping is fragile.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] After resume or close-and-start-new, `stale_dialog` is None and the dialog is dismissed, in gi-present headless runs too
- [ ] `uv run pytest tests/test_observing_widgets.py -q` passes with gi importable; full suite green; ty + ruff clean

## Comments

Audit finding (kind: bad-implementation, severity: medium). Fix direction: clear `_stale_dialog` directly in the button callbacks (or emit "closed" synchronously on button action).
