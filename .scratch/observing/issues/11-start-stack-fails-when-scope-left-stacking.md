# Issue 11: `iscope_start_stack` fails with code 259 when the scope was left stacking — no recovery, no clear message

**Status:** needs-triage

## Found while
Live observing 2026-11-22, project `vw-cep` (`~/SeestarProjects/vw-cep/subs/20261122T175849980496Z/` and `.../20261122T182137664766Z/`). Both sessions: goto OK, then

```
scope_error on 'iscope_start_stack': code 259
error on entry 0: RpcError: RPC 'iscope_start_stack' (id=661) failed with code=259
park failed: RpcError: RPC 'scope_park' (id=663) failed with code=203
```

No sub ever lands; the run aborts. Root cause: the scope was still in a stacking state from the earlier NGC869 run that died with connection lost (scope left stacking, never stopped). The Seestar RPC returns 259 ("busy / already stacking") for `iscope_start_stack` in that state, and 203 for park while stacking.

## Expected
- The executor (or device facade) calls `stop_stack` before `start_stack` when the scope reports a stacking/busy mode, or retries once after a stop — so a stale stacking state does not kill the night.
- The error surfaced in the UI names the RPC state problem in plain words ("scope was still stacking — stopped it, retrying" / "scope busy, stop stacking on the scope first"), not just `code=259`.

## Repro
1. Start an observation, kill the connection mid-stack (or stop the app while the scope stacks).
2. Reconnect, start a new observation in a new project.
3. `iscope_start_stack` fails with code 259; no subs are stored; park also fails (203).

Workaround for the user: in the Seestar phone app, stop stacking / set the scope to idle, then retry in the controller.

## Scope
`src/seestar_controller/observing/executor.py` (entry start: check `device.state().mode`, call `stop_stack` if stacking before `start_stack`), `src/seestar_controller/link/device.py` (error mapping for codes 259/203), `src/seestar_controller/observing/observe_view.py` (notification text), tests in `tests/test_observing_executor.py`.
