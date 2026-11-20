"""Protocol log for the Link: every JSON-RPC message on the command channel
(TCP :4700), with direction and timestamp, in a rotating log file.

The protocol is reverse-engineered (seestar_alp / seestarpy message shapes),
so a wire-level log is the main debugging aid when something misbehaves
against the real Scope. :class:`ProtocolLog` writes one JSON object per
line::

    {"ts": "2026-11-20T12:34:56.789012+00:00", "dir": "tx",
     "kind": "request", "msg": {"id": 2, "method": "verify_client", ...}}

- ``dir`` is ``"tx"`` (client -> scope) or ``"rx"`` (scope -> client);
  link lifecycle events (connection lost / reconnecting / ...) are logged
  as ``dir: "local"`` so a reconnect gap is visible in the log;
- ``kind`` is ``request`` / ``reply`` / ``event`` (wire) or ``lifecycle``;
- ``ts`` is the UTC timestamp of the log write.

**Key material is never logged.** The signed handshake sends the RSA
signature in ``params.sign`` and the key itself is PEM — both are redacted
before anything hits the disk: any field named ``sign`` / ``signature`` /
``pem`` / ``private_key`` / ``key_envelope`` (case-insensitive) is replaced
with ``"[REDACTED]"``, and PEM private-key blocks inside string values are
replaced with ``"[REDACTED PEM]"`` (see :func:`redact`).

The log lives under the config dir (``~/.config/seestar-controller/``, the
same dir as the PEM key) and rotates via :class:`logging.handlers.RotatingFileHandler`
(default 1 MiB per file, 3 backups). It is toggleable:

- explicitly, by passing a :class:`ProtocolLog` instance (or ``True`` /
  ``False``) as the transport's ``protocol_log=`` argument, optionally with
  ``protocol_log_path=``;
- via the environment: ``SEESTAR_PROTOCOL_LOG=1|0`` (default **on**) and
  ``SEESTAR_PROTOCOL_LOG_PATH=<path>``.

Resolution order mirrors the key path (see
:mod:`seestar_controller.link.keys`): explicit argument > env var > default.

NOTE (user AFK, no real scope — docs/wiki/assumptions.md): the log is
verified against the mock Scope's traffic only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Self

#: Config dir of the app (same dir as the PEM key).
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "seestar-controller"
#: Default protocol log file, under the config dir.
DEFAULT_LOG_PATH = DEFAULT_CONFIG_DIR / "protocol.log"

#: Environment variable that toggles the protocol log ("1"/"true"/"on" vs
#: "0"/"false"/"off"); when unset the log is ON.
LOG_ENABLED_ENV_VAR = "SEESTAR_PROTOCOL_LOG"
#: Environment variable that overrides the log file path.
LOG_PATH_ENV_VAR = "SEESTAR_PROTOCOL_LOG_PATH"

#: Rotation defaults: 1 MiB per file, 3 backups.
DEFAULT_MAX_BYTES = 1_000_000
DEFAULT_BACKUP_COUNT = 3

#: Placeholder written instead of redacted key material.
REDACTED = "[REDACTED]"
#: Placeholder written instead of a PEM private-key block.
REDACTED_PEM = "[REDACTED PEM]"

#: Message fields that must never reach the log (key material: signatures,
#: PEM keys, the 9.75 key envelope). Matched case-insensitively.
REDACTED_KEYS = frozenset(
    {"sign", "signature", "pem", "private_key", "private_key_pem", "key_pem", "key_envelope"}
)

#: A PEM private-key block inside a string value.
PEM_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)

_TRUE_WORDS = {"1", "true", "yes", "on"}
_FALSE_WORDS = {"0", "false", "no", "off"}


def redact(value: Any) -> Any:
    """Deep-copy ``value`` with key material removed.

    Dict keys named in :data:`REDACTED_KEYS` (case-insensitive) become
    ``"[REDACTED]"``; PEM private-key blocks inside strings become
    ``"[REDACTED PEM]"``. Everything else is copied unchanged.
    """
    if isinstance(value, Mapping):
        return {
            key: REDACTED if str(key).lower() in REDACTED_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return PEM_BLOCK_RE.sub(REDACTED_PEM, value)
    return value


def _parse_bool(raw: str) -> bool:
    word = raw.strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ValueError(
        f"{LOG_ENABLED_ENV_VAR} must be one of "
        f"{sorted(_TRUE_WORDS | _FALSE_WORDS)}, got {raw!r}"
    )


def log_enabled(setting: bool | None = None, *, env: Mapping[str, str] | None = None) -> bool:
    """Resolve whether the protocol log is enabled.

    Explicit ``setting`` (bool) wins; otherwise the ``SEESTAR_PROTOCOL_LOG``
    env var; otherwise the default is **on**.
    """
    if setting is not None:
        return bool(setting)
    environ = os.environ if env is None else env
    raw = environ.get(LOG_ENABLED_ENV_VAR)
    if raw is None:
        return True
    return _parse_bool(raw)


def resolve_log_path(
    path: str | Path | None = None, *, env: Mapping[str, str] | None = None
) -> Path:
    """Resolve the protocol log file path.

    Precedence: explicit ``path`` > ``SEESTAR_PROTOCOL_LOG_PATH`` env var >
    the default under the config dir (:data:`DEFAULT_LOG_PATH`).
    """
    if path is not None:
        return Path(path)
    environ = os.environ if env is None else env
    value = environ.get(LOG_PATH_ENV_VAR)
    if value:
        return Path(value)
    return Path(DEFAULT_LOG_PATH)


class ProtocolLog:
    """A rotating on-disk log of the Link's JSON-RPC traffic.

    One JSON object per line (see module docstring). Thread-safe; the file
    is opened lazily on the first :meth:`log` call and re-opened after
    :meth:`close`, so a transport can close the log on disconnect and keep
    logging if it reconnects later.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
        enabled: bool = True,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError(f"max_bytes must be > 0, got {max_bytes}")
        if backup_count < 0:
            raise ValueError(f"backup_count must be >= 0, got {backup_count}")
        self.path = Path(path)
        self.max_bytes = int(max_bytes)
        self.backup_count = int(backup_count)
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        # A private logger per instance: records never propagate to the root
        # logger, and the formatter is the bare message (one JSON per line).
        self._logger = logging.getLogger(f"seestar-controller.protocol-log.{id(self):x}")
        self._logger.propagate = False
        self._logger.setLevel(logging.INFO)
        self._handler: RotatingFileHandler | None = None

    # -- writing -----------------------------------------------------------

    def log(self, direction: str, kind: str, message: Mapping[str, Any]) -> None:
        """Write one traffic record: ``direction`` ("tx"/"rx"/"local"),
        ``kind`` ("request"/"reply"/"event"/"lifecycle"), ``message`` the
        (redacted) envelope."""
        if not self.enabled:
            return
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "dir": direction,
            "kind": kind,
            "msg": redact(message),
        }
        line = json.dumps(record, default=str, ensure_ascii=False)
        with self._lock:
            self._ensure_handler()
            # No %-interpolation: logger.info(line) with no args is literal.
            self._logger.info(line)
            if self._handler is not None:
                self._handler.flush()

    def _ensure_handler(self) -> None:
        if self._handler is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                self.path,
                maxBytes=self.max_bytes,
                backupCount=self.backup_count,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)
            self._handler = handler

    # -- lifecycle -----------------------------------------------------------

    def flush(self) -> None:
        with self._lock:
            if self._handler is not None:
                self._handler.flush()

    def close(self) -> None:
        """Flush and close the log file; the next :meth:`log` re-opens it."""
        with self._lock:
            if self._handler is not None:
                self._logger.removeHandler(self._handler)
                self._handler.close()
                self._handler = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def resolve_protocol_log(
    setting: bool | ProtocolLog | None = None,
    *,
    path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> ProtocolLog | None:
    """Resolve the transport's protocol log from a flag/env.

    ``setting`` may be a ready :class:`ProtocolLog` (used as-is), a bool
    (explicit on/off), or ``None`` (auto: ``SEESTAR_PROTOCOL_LOG`` env var,
    default on). Returns ``None`` when the log is disabled.
    """
    if isinstance(setting, ProtocolLog):
        return setting
    if not log_enabled(setting, env=env):
        return None
    return ProtocolLog(resolve_log_path(path, env=env))
