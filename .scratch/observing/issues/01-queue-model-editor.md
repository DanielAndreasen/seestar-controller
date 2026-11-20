# 01: Queue model + editor

**What to build:** tonight's plan as an editable queue — entries with name or RA/Dec (names resolved via catalog), mosaic, exposure, gain; add, reorder, remove, duplicate. Demoable: build and edit a queue in the Observe view.

**Blocked by:** ui-shell 01, catalog 01

**Status:** implemented in 4d7f3b4

- [x] Queue entries hold name-or-coords, mosaic, exposure, gain
- [x] Add by name resolves via catalog; add by RA/Dec validates input
- [x] Reorder / remove / duplicate entries in the UI
- [x] Queue persists for the session (not necessarily to disk yet)
- [x] Tests: model operations + name resolution with mocked catalog

## Comments

New package `src/seestar_controller/observing/` with the strict pure/widget split (subprocess + source-scan enforced, mirroring ui-shell 05):

- Pure `observing/queue.py` (stdlib + catalog only, no gi, no link): `QueueEntry` frozen dataclass `{name, coord, mosaic, exposure_s, gain}` — exactly one of name/coord at construction, all fields validated in `__post_init__` raising `QueueError(ValueError)`; ranges mirror the link's stacking wire contract (exposure 1–60 s, gain 0–100, mosaic pan/tilt 0–10°, re-declared like ui/state.py does). `PlanQueue` with add/remove/duplicate/move/move_up/move_down/clear, `len`/`[]`/`entries` tuple snapshot, and listeners notified with a frozen `QueueEvent` (same pattern as the ui controllers). `resolve_names(queue, resolver=...)` fills coords from names via an injectable resolver (default: lazy `catalog.facade.lookup_name` wrapper, brightest first); per-entry failures are collected into a `ResolutionReport` and the entry stays unresolved — never raises away the queue. `parse_entry_coords` wraps the catalog's `parse_coords`/`CoordinateError` into `QueueError`.
- Pure `observing/queue_controller.py`: `PlanQueueController` (queue + resolver + listener forwarding) with `add_by_name`/`add_by_coords`/`add_resolved`/`remove`/`duplicate`/`move*`/`clear`/`resolve_name`/`resolve_names` — the app-level shared instance, like `ConnectionController`.
- Widget `observing/queue_editor.py` (gi at top, lazy PEP 562 export): `QueueEditor` — Gtk.ListBox of `entry.describe()` rows with per-row up/down/duplicate/remove buttons, plus an Add area (name + Resolve button showing the catalog result/error inline, RA/Dec entries, mosaic switch + pan/tilt spins, exposure/gain spins with the validated ranges); re-renders on every queue event.
- Wiring: `SeestarApplication` owns `queue_controller = PlanQueueController()`; `SeestarWindow` takes/passes it to `build_view(view.id, queue_controller=...)`; `ObserveView` embeds the `QueueEditor` (plus a preview placeholder for later tickets).
- Tests: `tests/test_observing_queue.py` (105 pure headless: entry validation, queue ops + listener events, snapshot immutability, resolve_names with mocked resolver/monkeypatched facade/raising resolver, real `parse_ra`/`parse_dec` paths, controller, subprocess + source-scan boundary, ui_harness integration) and `tests/test_observing_widgets.py` (importorskip gi; skips in sandbox, runs on desktop/xvfb). Full suite: 1017 passed + 2 skipped.

Assumptions: queue is in-memory for the session only (per ticket); gain stored as int (wire contract sends int); a resolved entry carries both name+coord via `QueueEntry.resolved()`; default exposure 10 s / gain 50; stacking ranges are the mock's wire contract, unverified against real hardware; all link interaction stays mock-only (user AFK).
