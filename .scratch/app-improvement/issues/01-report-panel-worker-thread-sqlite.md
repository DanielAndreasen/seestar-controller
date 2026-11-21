# 01: Session report Generate/PDF always fail — worker thread uses the main-thread sqlite connection

**What to build:** make the session report panel's "Generate report" and "Export PDF" buttons actually produce `reports/<session>.html` / `.pdf` in the app (they currently fail with `ReportError: ... SQLite objects created in a thread can only be used in that same thread` and write nothing), and make the three widget tests pass in a gi-present environment.

Evidence: `src/seestar_controller/observing/report_panel.py:224-252` — `_run_worker` runs `generate_report`/`generate_pdf` on a daemon worker thread with `self._project`, whose sqlite connection was created on the main thread (`check_same_thread=True` default, `src/seestar_controller/store/project.py:297`). Failing tests: `tests/test_report_widgets.py::test_generate_button_saves_and_opens_the_html_and_lists_it`, `::test_pdf_button_produces_a_non_empty_pdf_next_to_the_html`, `::test_saved_report_row_activation_opens_the_file`. The repo's own convention is that store calls are marshalled to the db-owning thread via `invoke_on_main`/`GLib.idle_add` — see `src/seestar_controller/photometry/run.py` (`_call_store`) and the Projects view, which runs `generate_report` on the main loop.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] Clicking "Generate report" in the GUI writes a non-empty `reports/<session>.html`, lists it in the saved-reports box, and opens it
- [ ] Clicking "Export PDF" writes a non-empty `.pdf` next to the HTML
- [ ] `uv run pytest tests/test_report_widgets.py -q` passes with gi importable (no skips hiding failures)
- [ ] Full suite `uv run pytest -q -n auto` stays green; `uv tool run ty check` and `uv tool run ruff check` clean

## Comments

Audit finding (kind: bad-implementation, severity: high). Root-caused by live probe: same-thread call succeeds, worker-thread call raises the sqlite ProgrammingError. Fix direction: collect report data on the db-owning thread via `invoke_on_main` and keep only rendering on the worker, or open a fresh store connection on the worker thread.
