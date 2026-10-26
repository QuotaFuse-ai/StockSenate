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
import re
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator

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


def redact(value: Any) -> Any:
    """Recursively mask credentials in mappings, sequences and strings."""
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                cleaned[key] = _MASK
            else:
                cleaned[key] = redact(item)
        return cleaned
    if isinstance(value, (list, tuple, set)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE_RE.sub(_MASK, value)
    if isinstance(value, BaseException):
        return _SECRET_VALUE_RE.sub(_MASK, str(value))
    return value


class JsonFormatter(logging.Formatter):
    """One JSON object per line; safe to ship to a log collector."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
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
    if not logger.handlers:
        handler = logging.StreamHandler(stream or sys.stderr)
        if json_output:
            handler.setFormatter(JsonFormatter())
        else:
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
            )
        logger.addHandler(handler)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    base = logging.getLogger(LOGGER_NAME)
    if not base.handlers:
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
    "get_logger",
    "get_trace_id",
    "new_trace_id",
    "redact",
    "reset_trace_id",
    "set_trace_id",
    "stdout_to_stderr",
]
