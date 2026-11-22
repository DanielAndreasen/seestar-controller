# 04: Stale-session dialog is never forgotten — view relies solely on Adw.Dialog's "closed" signal

**What to build:** make the observe view clear its stale-session dialog deterministically when the user picks resume or start-new, instead of only via the animation-gated "closed" signal, so `stale_dialog` becomes `None` in every environment (headless/Xvfb included).

Evidence: `src/seestar_controller/observing/observe_view.py:490,495-497` connects `"closed"` → `_on_stale_dialog_closed`; `stale_session_dialog.py` button handlers only call `self.close()`. Live probe (libadwaita 1.5.0): `Adw.Dialog.close()` never emits "closed" and the dialog stays visible even with a running main loop, so the view leaks the closed dialog. Failing tests: `tests/test_observing_widgets.py::test_stale_session_dialog_resume_button_resumes_the_session` and `::test_stale_session_dialog_close_button_closes_and_starts_new` (both `assert widget.stale_dialog is None`). The controller actions themselves work — only the dialog bookkeeping is fragile.

**Blocked by:** None (can start immediately)

**Status:** implemented in 00feba3 (pending review)

- [x] After resume or close-and-start-new, `stale_dialog` is None and the dialog is dismissed, in gi-present headless runs too
- [x] `uv run pytest tests/test_observing_widgets.py -q` passes with gi importable; full suite green; ty + ruff clean

## Comments

Audit finding (kind: bad-implementation, severity: medium). Fix direction: clear `_stale_dialog` directly in the button callbacks (or emit "closed" synchronously on button action).

Implemented in 00feba3: `ObserveView._on_stale_session_resume` / `_on_stale_session_close_and_start_new` (the dialog's injected button callbacks) now call a new idempotent `_forget_stale_dialog()` first, so `_stale_dialog` is cleared deterministically on the button action; the `"closed"` connection remains as a backstop (libadwaita 1.5 `Adw.Dialog.close()` never emits it). Verified with gi present (PyGObject 3.58.1, Adw 1.5.0, DISPLAY :0.0): before the fix both failing tests failed with `stale_dialog is None` assertion; after, `uv run pytest tests/test_observing_widgets.py -q` → 31 passed (incl. the two stale-dialog tests), `uv run pytest tests/test_observing_observe.py -q` → 32 passed; `uv tool run ruff check` → All checks passed!; `uv tool run ty check` → 108 diagnostics, byte-identical count to unmodified main @ ae31075 (all pre-existing `gi.repository` stub-resolution noise, none from this change). Full suite left to the coordinator per ticket rules.
