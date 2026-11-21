# 11: movers-section report test is order-dependent (assumes pristine `sys.modules`)

**What to build:** make `test_movers_section_omitted_when_no_moving_objects_package` deterministic under `pytest -n auto`, where another test in the same worker may already have imported the moving-objects package.

Evidence: `tests/test_report_sections.py:271` asserts `"seestar_controller.moving_objects" not in sys.modules`; it passes standalone but fails in the full parallel run once any moving-objects test ran in the same worker. One of the two "known pre-existing failures" in `docs/wiki/dependency-upgrade.md`, but there attributed to headless-without-gi — in a gi-present environment it fails purely from in-worker module pollution.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] The test passes both standalone and in the full `uv run pytest -q -n auto` run (enforce isolation via monkeypatch/subprocess like the gi-boundary tests, or drop the sys.modules premise)
- [ ] Full suite green; ty + ruff clean

## Comments

Audit finding (kind: failing-test / test-isolation bug, severity: low). Test-side only; do not weaken the assertion's intent (movers section still omitted when no movers data exists).
