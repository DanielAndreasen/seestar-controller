"""Firmware-compat shim for the command channel (TCP :4700).

Firmware 9.75 renamed or removed several RPC methods the link layer used.
All facts below were live-probed against the Seestar S50 in station mode
(fw 9.75, 2026-11-20, PEM-signed handshake unchanged):

- ``get_scope_state`` -> code 103 (method not found). Replacement:
  ``iscope_get_app_state`` (no params) returns the full function tree;
  the ``View`` node carries ``state``/``mode``/``target_name``/
  ``target_ra_dec`` plus child ``Stack`` (``state``, ``lapse_ms``,
  ``stacked_frame``, ``dropped_frame``) and ``AutoGoto`` (the slew) nodes.
- ``scope_goto_target`` -> 103. Replacement: ``scope_goto`` with params
  ``{"ra": hours, "dec": deg}`` (positional ``[ra, dec]`` also accepted);
  wrong types -> 108, out of range -> 207, and no motion happens on
  errors. The ``ScopeGoto`` event is still pushed.
- ``iscope_start_stacking`` / ``iscope_stop_stacking`` -> 103. Replacements:
  ``iscope_start_stack`` with ``{"restart": bool, ...}`` (code 259 when
  already running and ``restart`` is true) and ``iscope_stop_view`` with
  ``{"stage": "Stack"}`` to stop just the stack (no params = full stop).
- ``list_frames`` / ``get_frame`` -> 103. Replacement flow: ``get_albums``
  (no params) lists folders directly under ``MyWorks`` (sub-frame folders
  end in ``_sub``), then ``get_img_file_page_number`` with
  ``{"dir": "MyWorks/<folder>", "skip_avi": false}`` gives the page count
  (20 per page) and ``get_img_file_page_name`` with ``{"page": int}``
  (stateful cursor set by the page-number call) lists the files; page out
  of range -> 204. Downloads are plain HTTP GET
  ``http://<ip>/<url-quoted MyWorks/... path>`` — see :mod:`.frames`.

Strategy: try the 9.75 name first; on :class:`~.transport.RpcError` code
103 (method not found) fall back to the legacy name; cache the working
variant per transport so later calls skip the probe. Any other error code
(108, 207, ...) propagates immediately — those are real answers, not
version differences.
"""

from __future__ import annotations

import weakref
from typing import Any, Callable, Mapping, Sequence

from .transport import LinkTransport, RpcError

#: JSON-RPC error code the Scope returns for an unknown method name.
METHOD_NOT_FOUND_CODE = 103

# -- fw 9.75 method names --------------------------------------------------------

#: fw 9.75 replacement for ``get_scope_state`` (full app function tree).
GET_APP_STATE_METHOD = "iscope_get_app_state"
#: fw 9.75 replacement for ``scope_goto_target`` (params ``{"ra", "dec"}``).
GOTO_METHOD_975 = "scope_goto"
#: fw 9.75 replacement for ``iscope_start_stacking`` (params ``{"restart", ...}``).
START_STACK_METHOD_975 = "iscope_start_stack"
#: fw 9.75 replacement for ``iscope_stop_stacking`` (params ``{"stage": ...}``).
STOP_STACK_METHOD_975 = "iscope_stop_view"
#: fw 9.75 ``stage`` value that stops only the stack, not the whole view.
STOP_STACK_STAGE = "Stack"
#: fw 9.75 album listing (folders under ``MyWorks``); replaces ``list_frames``.
GET_ALBUMS_METHOD = "get_albums"
#: fw 9.75 page count for one album folder; params ``{"dir", "skip_avi"}``.
PAGE_NUMBER_METHOD = "get_img_file_page_number"
#: fw 9.75 page listing; params ``{"page"}`` (cursor set by PAGE_NUMBER_METHOD).
PAGE_NAME_METHOD = "get_img_file_page_name"

# -- method pairs: (fw 9.75 name, legacy name) ------------------------------------
# The legacy literals mirror device.GET_SCOPE_STATE_METHOD / GOTO_METHOD,
# stacking.START_STACK_METHOD / STOP_STACK_METHOD and frames.LIST_FRAMES_METHOD;
# they are spelled out here so this module stays import-cycle free.

STATE_METHODS = (GET_APP_STATE_METHOD, "get_scope_state")
GOTO_METHODS = (GOTO_METHOD_975, "scope_goto_target")
START_STACK_METHODS = (START_STACK_METHOD_975, "iscope_start_stacking")
STOP_STACK_METHODS = (STOP_STACK_METHOD_975, "iscope_stop_stacking")
FRAMES_LIST_METHODS = (GET_ALBUMS_METHOD, "list_frames")

#: Error code returned when a stack is started with restart=true while running.
ALREADY_RUNNING_CODE = 259
#: Error code for a page index beyond the last page of an album folder.
OUT_OF_LIMIT_CODE = 204

#: Per-transport cache: {methods tuple -> winning method name}.
#: Weak keys so a disconnected transport can be garbage collected.
_cache: "weakref.WeakKeyDictionary[Any, dict[tuple[str, ...], str]]" = (
    weakref.WeakKeyDictionary()
)


def _cache_for(transport: LinkTransport) -> dict[tuple[str, ...], str]:
    return _cache.setdefault(transport, {})


def cached_method(
    transport: LinkTransport, methods: Sequence[str]
) -> str | None:
    """The method name that last succeeded for ``methods`` on ``transport``."""
    return _cache_for(transport).get(tuple(methods))


def reset_cache(transport: LinkTransport | None = None) -> None:
    """Forget cached method winners (one transport, or all)."""
    if transport is None:
        _cache.clear()
    else:
        _cache.pop(transport, None)


def call(
    transport: LinkTransport,
    methods: Sequence[str],
    params_for: Callable[[str], Mapping[str, Any] | None] | None = None,
    *,
    timeout: float | None = None,
) -> tuple[str, Any]:
    """Call the first of ``methods`` the Scope actually implements.

    ``methods`` is ordered newest-first (fw 9.75 name, then legacy). The
    cached winner is tried first; a code-103 reply moves on to the next
    candidate; any other non-zero code raises :class:`RpcError` immediately.
    ``params_for(method)`` builds the params per candidate (they differ
    between firmware versions). Returns ``(method, result)`` and caches
    the winner on ``transport``.
    """
    methods = tuple(methods)
    if not methods:
        raise ValueError("methods must not be empty")
    cache = _cache_for(transport)
    if methods in cache:
        winner = cache[methods]
        order = [winner] + [m for m in methods if m != winner]
    else:
        order = list(methods)
    reply = None
    for method in order:
        params = params_for(method) if params_for is not None else None
        # A 103 here is an expected dialect probe, not a scope error: keep it
        # off the scope_error lifecycle event so observers do not react to it.
        reply = transport.request_raw(
            method, params, timeout=timeout, suppress_scope_error=True
        )
        if reply.ok:
            cache[methods] = method
            return method, reply.result
        if reply.code != METHOD_NOT_FOUND_CODE:
            raise RpcError(reply)
    # every candidate answered 103 — drop a stale cache entry and surface it
    cache.pop(methods, None)
    assert reply is not None
    raise RpcError(reply)
