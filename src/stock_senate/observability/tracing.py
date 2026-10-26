"""OpenTelemetry- and Langfuse-compatible tracing.

Tracing is off by default. When enabled, every pipeline stage and every model
call becomes a span carrying the run's ``trace_id`` from
``observability.logging``, so a log line and a trace can be matched by id.

Export is best-effort. A missing endpoint, an unreachable collector or a
broken transport is logged once and never fails a research run: the pipeline
result is identical with tracing on or off.

Two exporters are built in:

* ``jsonl`` appends one JSON object per span to a local file. No extra
  dependency, no network; this is the default when tracing is enabled.
* ``otlp`` posts OTLP/HTTP JSON. Point ``endpoint`` at an OpenTelemetry
  Collector, or at Langfuse's OTLP endpoint
  (``https://cloud.langfuse.com/api/public/otel``). ``headers_env`` names an
  environment variable holding ``key=value,key=value`` auth pairs, so an
  ingestion secret never has to live in ``workflow.toml``.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from stock_senate.observability.logging import get_trace_id, redact
from stock_senate.version import __version__

log = logging.getLogger("stock_senate.tracing")

EXPORTERS = ("none", "jsonl", "otlp")
DEFAULT_PATH = "~/.stock_senate/traces/spans.jsonl"
DEFAULT_HEADERS_ENV = "STOCKSENATE_OTEL_HEADERS"
_MAX_ERROR = 400


def _now_ns() -> int:
    return time.time_ns()


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=UTC).isoformat(timespec="milliseconds")


@dataclass
class Span:
    """One timed unit of work; plain data so any exporter can consume it."""

    name: str
    trace_id: str
    span_id: str
    parent_id: str | None
    start_ns: int
    end_ns: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"
    error: str = ""

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[str(key)] = value

    def set_attributes(self, values: Mapping[str, Any]) -> None:
        for key, value in values.items():
            self.set_attribute(key, value)

    def fail(self, exc: BaseException) -> None:
        self.status = "error"
        self.error = str(redact(str(exc)))[:_MAX_ERROR]

    def as_dict(self) -> dict[str, Any]:
        end = self.end_ns or _now_ns()
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "started_at": _iso(self.start_ns),
            "ended_at": _iso(end),
            "duration_ms": round((end - self.start_ns) / 1_000_000, 3),
            "status": self.status,
            "error": self.error,
            "attributes": dict(self.attributes),
        }


class Exporter(Protocol):
    def export(self, span: Span) -> None: ...

    def flush(self) -> None: ...


class JsonlExporter:
    """Append spans as JSON lines; no extra dependency and no network."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, span: Span) -> None:
        line = json.dumps(redact(span.as_dict()), ensure_ascii=False, default=str)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def flush(self) -> None:
        return None


def _attr(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


class OtlpExporter:
    """OTLP/HTTP JSON exporter, compatible with a Collector or Langfuse.

    Spans are buffered and posted in batches; the tracer flushes when the
    outermost span ends. ``transport`` is injectable so tests never touch the
    network.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        headers: Mapping[str, str] | None = None,
        service_name: str = "stocksenate",
        timeout: float = 5.0,
        max_batch: int = 64,
        transport: Callable[[str, bytes, Mapping[str, str]], None] | None = None,
    ) -> None:
        target = str(endpoint or "").strip().rstrip("/")
        if not target:
            raise ValueError("otlp 导出器需要 endpoint（Collector 或 Langfuse 的 OTLP 地址）")
        self.url = target if target.endswith("/v1/traces") else f"{target}/v1/traces"
        self.headers = dict(headers or {})
        self.service_name = service_name or "stocksenate"
        self.timeout = max(0.1, float(timeout))
        self.max_batch = max(1, int(max_batch))
        self._transport = transport or self._post
        self._buffer: list[Span] = []

    def export(self, span: Span) -> None:
        self._buffer.append(span)
        if len(self._buffer) >= self.max_batch:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        spans, self._buffer = self._buffer, []
        body = json.dumps(self.payload(spans), ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", **self.headers}
        self._transport(self.url, body, headers)

    def payload(self, spans: list[Span]) -> dict[str, Any]:
        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            _attr("service.name", self.service_name),
                            _attr("service.version", __version__),
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "stock_senate"},
                            "spans": [self._span(span) for span in spans],
                        }
                    ],
                }
            ]
        }

    def _span(self, span: Span) -> dict[str, Any]:
        end = span.end_ns or _now_ns()
        return {
            "traceId": span.trace_id,
            "spanId": span.span_id,
            "parentSpanId": span.parent_id or "",
            "name": span.name,
            "kind": 1,
            "startTimeUnixNano": str(span.start_ns),
            "endTimeUnixNano": str(end),
            "attributes": [_attr(key, value) for key, value in span.attributes.items()],
            "status": {"code": 2 if span.status == "error" else 1, "message": span.error},
        }

    def _post(self, url: str, body: bytes, headers: Mapping[str, str]) -> None:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            response.read(1)


_current_span: ContextVar[Span | None] = ContextVar("stock_senate_current_span", default=None)


class Tracer:
    """Create spans from the active ``trace_id`` and hand them to an exporter."""

    def __init__(self, exporter: Exporter | None = None) -> None:
        self.exporter = exporter
        self._export_failed = False

    @property
    def enabled(self) -> bool:
        return self.exporter is not None

    @contextmanager
    def span(self, name: str, attributes: Mapping[str, Any] | None = None) -> Iterator[Span]:
        parent = _current_span.get()
        current = Span(
            name=str(name),
            trace_id=get_trace_id(),
            span_id=uuid.uuid4().hex[:16],
            parent_id=parent.span_id if parent is not None else None,
            start_ns=_now_ns(),
            attributes=dict(attributes or {}),
        )
        token = _current_span.set(current)
        try:
            yield current
        except BaseException as exc:
            current.fail(exc)
            raise
        finally:
            _current_span.reset(token)
            current.end_ns = _now_ns()
            self._export(current)
            if parent is None:
                self.flush()

    def flush(self) -> None:
        if self.exporter is None:
            return
        try:
            self.exporter.flush()
        except Exception as exc:  # noqa: BLE001 - a sink never fails a run
            self._warn_once(exc)

    def _export(self, span: Span) -> None:
        if self.exporter is None:
            return
        try:
            self.exporter.export(span)
        except Exception as exc:  # noqa: BLE001 - a sink never fails a run
            self._warn_once(exc)

    def _warn_once(self, exc: BaseException) -> None:
        if self._export_failed:
            return
        self._export_failed = True
        log.warning("trace export failed; continuing best-effort: %s", redact(str(exc)))


_tracer = Tracer()


def get_tracer() -> Tracer:
    return _tracer


def set_tracer(tracer: Tracer | None) -> None:
    global _tracer
    _tracer = tracer or Tracer()


@contextmanager
def span(name: str, attributes: Mapping[str, Any] | None = None) -> Iterator[Span]:
    with _tracer.span(name, attributes) as current:
        yield current


def _timeout(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 5.0
    return parsed if parsed > 0 else 5.0


def tracing_settings(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Merge ``workflow.tracing`` with the ``STOCKSENATE_TRACE_*`` overrides."""
    section = ((config or {}).get("workflow") or {}).get("tracing") or {}
    env_exporter = str(os.environ.get("STOCKSENATE_TRACE_EXPORTER") or "").strip().lower()
    exporter = env_exporter or str(section.get("exporter") or "none").strip().lower()
    return {
        "enabled": bool(env_exporter) or section.get("enabled") is True,
        "exporter": exporter,
        "path": str(
            os.environ.get("STOCKSENATE_TRACE_FILE") or section.get("path") or DEFAULT_PATH
        ),
        "endpoint": str(
            os.environ.get("STOCKSENATE_OTEL_ENDPOINT") or section.get("endpoint") or ""
        ),
        "service_name": str(section.get("service_name") or "stocksenate"),
        "headers_env": str(section.get("headers_env") or DEFAULT_HEADERS_ENV),
        "timeout": _timeout(section.get("timeout_seconds")),
    }


def _auth_headers(name: str) -> dict[str, str]:
    raw = os.environ.get(name, "") if name else ""
    headers: dict[str, str] = {}
    for part in raw.split(","):
        key, _, value = part.partition("=")
        if key.strip() and value.strip():
            headers[key.strip()] = value.strip()
    return headers


def _build_exporter(settings: Mapping[str, Any]) -> Exporter | None:
    if not settings["enabled"]:
        return None
    kind = str(settings["exporter"])
    if kind == "none":
        return None
    if kind == "jsonl":
        return JsonlExporter(str(settings["path"]))
    if kind == "otlp":
        return OtlpExporter(
            str(settings["endpoint"]),
            headers=_auth_headers(str(settings["headers_env"])),
            service_name=str(settings["service_name"]),
            timeout=float(settings["timeout"]),
        )
    raise ValueError(f"未知 tracing.exporter: {kind!r}")


def configure_tracing(config: Mapping[str, Any] | None) -> str:
    """Install the exporter described by ``workflow.tracing``; never raises.

    Returns the active exporter name, or ``"disabled"``. A configuration that
    cannot be honoured (unknown exporter, missing endpoint, unwritable path)
    logs a warning and leaves tracing off instead of failing the run.
    """
    try:
        settings = tracing_settings(config)
        if settings["enabled"]:
            path = Path(str(settings["path"])).expanduser()
            if not path.is_absolute():
                base = str((config or {}).get("home") or "").strip()
                path = (Path(base).expanduser() if base else Path.cwd()) / path
            settings["path"] = str(path)
        exporter = _build_exporter(settings)
    except Exception as exc:  # noqa: BLE001 - observability never breaks a run
        log.warning("tracing disabled: %s", redact(str(exc)))
        set_tracer(None)
        return "disabled"
    set_tracer(Tracer(exporter))
    return str(settings["exporter"]) if exporter is not None else "disabled"


def reset_tracing() -> None:
    set_tracer(None)


__all__ = [
    "DEFAULT_HEADERS_ENV",
    "DEFAULT_PATH",
    "EXPORTERS",
    "Exporter",
    "JsonlExporter",
    "OtlpExporter",
    "Span",
    "Tracer",
    "configure_tracing",
    "get_tracer",
    "reset_tracing",
    "set_tracer",
    "span",
    "tracing_settings",
]
