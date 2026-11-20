"""Wire contract for stacking control, shared by the facade and the mock Scope.

The Scope stacks **sub-frames** (individual exposures, GLOSSARY.md) into a
**Stack**. On the wire (seestar_alp / seestarpy message shapes):

- start: ``iscope_start_stacking`` with params ``exp_ms`` (sub-frame
  exposure in milliseconds), ``gain``, ``is_mosaic`` and the optional mosaic
  framing ``mosaic_pan`` / ``mosaic_tilt`` (degrees);
- stop: ``iscope_stop_stacking``;
- progress: pushed ``Stack`` events with ``state`` ("working" while
  stacking, "idle" once stopped), ``lapse_ms`` (elapsed) and ``frames``
  (sub-frames stacked so far).

On **fw 9.75** (live-probed against the Seestar S50, station mode,
2026-11-20 — see :mod:`.compat`) both RPCs answer code 103; the replacements
are ``iscope_start_stack`` with ``{"restart": bool, ...same params}`` (code
259 when already running and ``restart`` is true) and ``iscope_stop_view``
with ``{"stage": "Stack"}`` (stops just the stack; no params = full stop).

Both the facade (:mod:`seestar_controller.link.device`) and the mock Scope
(:mod:`seestar_controller.link.mock_scope`) validate against the ranges
defined here, so invalid parameters are rejected client-side with a
``ValueError`` *and* on the wire with a non-zero reply code.

NOTE: the fw 9.75 method names and error codes were live-verified against a
real Seestar S50 (fw 9.75); the param ranges below are the mock's contract,
chosen to match the S50's app-level limits, and remain unverified against
real hardware.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: RPC that begins stacking a Stack on the current target.
START_STACK_METHOD = "iscope_start_stacking"
#: RPC that stops the current Stack.
STOP_STACK_METHOD = "iscope_stop_stacking"
#: Pushed event carrying stack progress (frames stacked, elapsed).
STACK_EVENT = "Stack"
#: ``Stack`` event state while the Stack is being collected.
STACK_STATE_WORKING = "working"
#: ``Stack`` event state of the final event once the Stack stopped — the
#: "stack finished" occurrence (dispatched as ``stack_finished``).
STACK_STATE_IDLE = "idle"

#: Sub-frame exposure range accepted by the Scope, in seconds.
EXPOSURE_MIN_S = 1.0
EXPOSURE_MAX_S = 60.0
#: Gain range accepted by the Scope.
GAIN_MIN = 0
GAIN_MAX = 100
#: Mosaic framing (pan/tilt) range, in degrees.
MOSAIC_MIN_DEG = 0.0
MOSAIC_MAX_DEG = 10.0


def exposure_to_exp_ms(exposure: float) -> int:
    """Convert a sub-frame exposure in seconds to wire ``exp_ms``."""
    return round(float(exposure) * 1000.0)


def normalize_mosaic(mosaic: Any) -> tuple[bool, float | None, float | None]:
    """Normalize the ``mosaic`` argument to ``(is_mosaic, pan, tilt)``.

    ``mosaic`` may be a bool (mosaic framing off/on with the Scope's default
    geometry) or a framing given as a ``(pan, tilt)`` pair / mapping with
    ``pan`` / ``tilt`` keys, in degrees. Raises ``ValueError`` on bad values.
    """
    if mosaic is None:
        mosaic = False
    if isinstance(mosaic, bool):
        return mosaic, None, None
    if isinstance(mosaic, Mapping):
        pan_raw, tilt_raw = mosaic.get("pan", 0.0), mosaic.get("tilt", 0.0)
    elif isinstance(mosaic, (tuple, list)):
        if len(mosaic) != 2:
            raise ValueError(
                f"mosaic framing must be a (pan, tilt) pair in degrees, got {mosaic!r}"
            )
        pan_raw, tilt_raw = mosaic
    else:
        raise ValueError(  # noqa: TRY004
            f"mosaic must be a bool or a (pan, tilt) framing in degrees, got {mosaic!r}"
        )
    try:
        pan, tilt = float(pan_raw), float(tilt_raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"mosaic pan/tilt must be numbers in degrees, got {mosaic!r}"
        ) from None
    for name, value in (("pan", pan), ("tilt", tilt)):
        if not MOSAIC_MIN_DEG <= value <= MOSAIC_MAX_DEG:
            raise ValueError(
                f"mosaic {name} must be in "
                f"[{MOSAIC_MIN_DEG}, {MOSAIC_MAX_DEG}] degrees, got {value}"
            )
    return True, pan, tilt


def stack_request_params(exposure: Any, gain: Any, mosaic: Any = False) -> dict[str, Any]:
    """Validate ``start_stack`` arguments and build the wire params.

    ``exposure`` is the sub-frame exposure in seconds. Raises ``ValueError``
    with a clear message on out-of-range values (nothing is sent then).
    """
    try:
        exposure_s = float(exposure)
    except (TypeError, ValueError):
        raise ValueError(f"exposure must be a number of seconds, got {exposure!r}") from None
    if not EXPOSURE_MIN_S <= exposure_s <= EXPOSURE_MAX_S:
        raise ValueError(
            f"exposure must be in [{EXPOSURE_MIN_S}, {EXPOSURE_MAX_S}] s, got {exposure_s}"
        )
    try:
        gain_value = float(gain)
    except (TypeError, ValueError):
        raise ValueError(f"gain must be a number, got {gain!r}") from None
    if not GAIN_MIN <= gain_value <= GAIN_MAX:
        raise ValueError(f"gain must be in [{GAIN_MIN}, {GAIN_MAX}], got {gain}")
    is_mosaic, pan, tilt = normalize_mosaic(mosaic)
    params: dict[str, Any] = {
        "exp_ms": exposure_to_exp_ms(exposure_s),
        "gain": int(gain_value),
        "is_mosaic": is_mosaic,
    }
    if pan is not None:
        params["mosaic_pan"] = pan
        params["mosaic_tilt"] = tilt
    return params


def wire_params_to_stack_args(params: Mapping[str, Any]) -> tuple[float, Any, Any]:
    """Convert raw wire params (``exp_ms`` ...) to ``start_stacking`` args.

    Returns ``(exposure_s, gain, mosaic)``. Raises ``ValueError`` when
    required params are missing or malformed.
    """
    if "exp_ms" not in params:
        raise ValueError("exp_ms (sub-frame exposure in ms) is required")
    if "gain" not in params:
        raise ValueError("gain is required")
    exposure_s = float(params["exp_ms"]) / 1000.0
    gain = params["gain"]
    raw_mosaic = params.get("is_mosaic", False)
    if not isinstance(raw_mosaic, bool):
        raise ValueError(f"is_mosaic must be a bool, got {raw_mosaic!r}")  # noqa: TRY004
    mosaic: Any = raw_mosaic
    if params.get("mosaic_pan") is not None or params.get("mosaic_tilt") is not None:
        mosaic = {
            "pan": params.get("mosaic_pan", 0.0),
            "tilt": params.get("mosaic_tilt", 0.0),
        }
    return exposure_s, gain, mosaic
