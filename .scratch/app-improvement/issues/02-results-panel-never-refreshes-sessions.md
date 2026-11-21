# 02: Offline photometry Run is a no-op — results panel never re-lists sessions after a project is opened

**What to build:** make the photometry results panel populate its session dropdown when the project is set after the view was built, so clicking Run measures the stored session without a manual Refresh.

Evidence: `src/seestar_controller/photometry/results_view.py:184-185` calls `refresh_sessions()` only in `__init__`; `_render` (results_view.py:462+) never refreshes the dropdown; `_on_run_clicked` (results_view.py:380-386) exits with "no session to run" when `selected_session_id()` is None. The window/views are built before any project is opened, so after `open_project`/`open_project_offline` the chooser stays empty. Sibling views already do this correctly: `moving_objects/overlay_view.py` and `frame_quality/quality_view.py` refresh the dropdown inside `_render` when `snapshot.sessions` changes. Failing test: `tests/test_offline_analysis_widgets.py::test_photometry_run_button_measures_the_stored_session_offline` (30 s pump timeout, "photometry run did not store the measurements"). This breaks offline-analysis issue 02's own acceptance ("run a photometry measurement … without any scope connection").

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] After opening a project (online or offline), the session dropdown lists its sessions and Run stores measurements for the selected session without clicking Refresh
- [ ] `uv run pytest tests/test_offline_analysis_widgets.py -q` passes with gi importable
- [ ] Full suite `uv run pytest -q -n auto` stays green; ty + ruff clean

## Comments

Audit finding (kind: bad-implementation / missing behavior, severity: high). Fix direction: in `_render`/`_on_snapshot`, refresh the dropdown when the controller's sessions differ from the listed ids (same pattern as MoverOverlayView).
