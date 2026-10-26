"""Structured logging with stdout/stderr separation and redaction.

Two rules are load-bearing:

* stdout is reserved for machine output (CLI results, MCP frames). Every log
  record goes to stderr.
* secrets never reach a sink. ``redact`` is applied to the whole record, so a
  key nested in an arbitrary ``detail`` dict is masked too.

``trace_id`` travels in a context variable so every log line of one run can be
correlated without threading an id through every function call.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import logging.handlers
import os
import re
import sys
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOGGER_NAME = "stock_senate"

_SECRET_KEY_RE = re.compile(
    # ``tokens`` (a budget count) is not a credential; ``token`` (singular) is.
    # ``*_env`` keys name an environment variable, they hold no secret value.
    r"(api[_-]?key|(?<![a-z])token(?![a-z])|secret(?!s?_env)"
    r"|password|passwd|authorization|dsn(?!_env)|credential)",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._~+/=-]{8,})",
    re.IGNORECASE,
)
# Path-like keys are only masked when their value is absolute: a relative
# ``root = "data"`` is not sensitive, and keeping it readable helps support.
_PATH_KEY_RE = re.compile(
    r"(^|_)(path|paths|root|roots|dir|dirs|directory|cwd|home|file|files|folder)(_|$)",
    re.IGNORECASE,
)
_ABS_PATH_RE = re.compile(
    r"""(?:[A-Za-z]:[\\/][^\s"'<>|]*)                  # C:\... or C:/...
    |(?:\\\\[^\s"'<>|\\/]+(?:[\\/][^\s"'<>|]+)+)       # \\host\share\...
    |(?<![\w:/])/(?:[^\s"'<>|/]+/)*[^\s"'<>|/]+        # /home/...
    """,
    re.VERBOSE,
)
_MASK = "***"

_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("stock_senate_trace_id", default="")


def new_trace_id() -> str:
    """Start a new trace and return its id (also used as ``run_id`` prefix)."""
    value = uuid.uuid4().hex
    _trace_id.set(value)
    return value


def get_trace_id() -> str:
    value = _trace_id.get()
    if not value:
        value = uuid.uuid4().hex
        _trace_id.set(value)
    return value


def set_trace_id(value: str) -> contextvars.Token[str]:
    return _trace_id.set(value)


def reset_trace_id(token: contextvars.Token[str]) -> None:
    _trace_id.reset(token)


def redact(value: Any, *, mask_paths: bool = False) -> Any:
    """Recursively mask credentials in mappings, sequences and strings.

    ``mask_paths`` additionally masks absolute local paths (``C:\\Users\\...``,
    ``/home/...``). It is opt-in because log records benefit from the path,
    while anything copied out of the process (``config show``, MCP results)
    should not carry the operator's home directory.
    """
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                cleaned[key] = _MASK
            elif (
                mask_paths
                and isinstance(key, str)
                and _PATH_KEY_RE.search(key)
                and isinstance(item, str)
                and _ABS_PATH_RE.search(item)
            ):
                cleaned[key] = _MASK
            else:
                cleaned[key] = redact(item, mask_paths=mask_paths)
        return cleaned
    if isinstance(value, (list, tuple, set)):
        return [redact(item, mask_paths=mask_paths) for item in value]
    if isinstance(value, str):
        masked = _SECRET_VALUE_RE.sub(_MASK, value)
        return _ABS_PATH_RE.sub(_MASK, masked) if mask_paths else masked
    if isinstance(value, BaseException):
        masked = _SECRET_VALUE_RE.sub(_MASK, str(value))
        return _ABS_PATH_RE.sub(_MASK, masked) if mask_paths else masked
    return value


class JsonFormatter(logging.Formatter):
    """One JSON object per line; safe to ship to a log collector."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", None) or _trace_id.get() or None,
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), ensure_ascii=False, default=str)


_RESERVED = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
    "message",
    "asctime",
}


def configure(
    level: str = "INFO",
    *,
    stream: Any | None = None,
    force: bool = False,
    json_output: bool = True,
) -> logging.Logger:
    """Attach a single stderr handler to the package logger."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False
    if force:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
    if not _sinks(logger, _STREAM_SINK):
        handler = logging.StreamHandler(stream or sys.stderr)
        if json_output:
            handler.setFormatter(JsonFormatter())
        else:
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
            )
        setattr(handler, _SINK_ATTR, _STREAM_SINK)
        logger.addHandler(handler)
    return logger


_SINK_ATTR = "_stock_senate_sink"
_STREAM_SINK = "stream"
_FILE_SINK = "file"


def _sinks(logger: logging.Logger, kind: str) -> list[logging.Handler]:
    return [h for h in logger.handlers if getattr(h, _SINK_ATTR, None) == kind]


def configure_file_sink(
    path: str | Path,
    *,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    json_output: bool = True,
) -> Path:
    """Mirror the package logger into a rotating UTF-8 file.

    Repeating the call with the same target is a no-op, so every run can call
    it. The JSON formatter is reused, which means the file gets the same
    redaction as stderr; nothing is ever written to stdout.
    """
    target = Path(path).expanduser()
    logger = logging.getLogger(LOGGER_NAME)
    resolved = str(target.resolve())
    for handler in _sinks(logger, _FILE_SINK):
        if getattr(handler, "baseFilename", None) == resolved:
            return target
        logger.removeHandler(handler)
        handler.close()
    target.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        target,
        maxBytes=max(1, int(max_bytes)),
        backupCount=max(0, int(backup_count)),
        encoding="utf-8",
    )
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    setattr(handler, _SINK_ATTR, _FILE_SINK)
    logger.addHandler(handler)
    return target


def configure_from_config(
    config: Mapping[str, Any] | None,
    *,
    json_output: bool = True,
) -> Path | None:
    """Enable the file sink when ``workflow.logging`` asks for it.

    Off by default: a stock install keeps logging to stderr only, and a test
    run never drops files into the operator's home. ``STOCKSENATE_LOG_FILE``
    enables the sink without touching any configuration file.
    """
    section = (((config or {}).get("workflow") or {}).get("logging")) or {}
    env_path = str(os.environ.get("STOCKSENATE_LOG_FILE") or "").strip()
    target = env_path or str(section.get("file_path") or "").strip()
    enabled = bool(env_path) or section.get("file_enabled") is True
    logger = logging.getLogger(LOGGER_NAME)
    if not enabled or not target:
        for handler in _sinks(logger, _FILE_SINK):
            logger.removeHandler(handler)
            handler.close()
        return None
    path = Path(target).expanduser()
    if not path.is_absolute():
        base = str((config or {}).get("home") or "").strip()
        path = (Path(base).expanduser() if base else Path.cwd()) / path
    return configure_file_sink(
        path,
        max_bytes=int(section.get("max_bytes") or 10 * 1024 * 1024),
        backup_count=int(section.get("backup_count") or 5),
        json_output=json_output,
    )


def get_logger(name: str | None = None) -> logging.Logger:
    base = logging.getLogger(LOGGER_NAME)
    if not _sinks(base, _STREAM_SINK):
        configure()
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def audit(event: str, **fields: Any) -> None:
    """Audit trail for configuration writes and permission decisions."""
    get_logger("audit").info(event, extra={"event": event, "audit": True, **fields})


@contextlib.contextmanager
def stdout_to_stderr() -> Iterator[None]:
    """Keep protocol stdout clean (MCP stdio, CLI piping)."""
    with contextlib.redirect_stdout(sys.stderr):
        yield


__all__ = [
    "JsonFormatter",
    "audit",
    "configure",
    "configure_file_sink",
    "configure_from_config",
    "get_logger",
    "get_trace_id",
    "new_trace_id",
    "redact",
    "reset_trace_id",
    "set_trace_id",
    "stdout_to_stderr",
]
