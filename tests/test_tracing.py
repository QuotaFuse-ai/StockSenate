"""Tracing: spans off by default, exported when asked for, never fatal.

The contract these tests pin:

* no exporter is installed unless ``workflow.tracing`` enables one, so a stock
  install writes nothing and opens no connection;
* every pipeline stage and every role model call becomes a span carrying the
  run's ``trace_id``, with model spans nested under their stage span;
* an exporter that raises is logged once and never changes the run result;
* ``jsonl`` writes one JSON object per span and ``otlp`` builds a payload a
  Collector or Langfuse accepts;
* ``STOCKSENATE_TRACE_*`` overrides win over the configuration file.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from stock_senate import SenateGraph
from stock_senate.observability.tracing import (
    JsonlExporter,
    OtlpExporter,
    Span,
    Tracer,
    configure_tracing,
    get_tracer,
    reset_tracing,
    set_tracer,
    span,
    tracing_settings,
)

CODE = "600519"
DAY = "2026-10-24"
STAGE_NAMES = {
    "stage.analysts",
    "stage.debate",
    "stage.research",
    "stage.trader",
    "stage.hitl_after_trader",
    "stage.risk",
    "stage.hitl_before_final",
    "stage.portfolio_manager",
    "stage.quality_gate",
}


class _Recorder:
    def __init__(self) -> None:
        self.spans: list[Span] = []
        self.flushes = 0

    def export(self, span: Span) -> None:
        self.spans.append(span)

    def flush(self) -> None:
        self.flushes += 1


class _Broken:
    def export(self, span: Span) -> None:
        raise RuntimeError("collector unreachable")

    def flush(self) -> None:
        raise RuntimeError("collector unreachable")


@pytest.fixture(autouse=True)
def _clean_tracing(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "STOCKSENATE_TRACE_EXPORTER",
        "STOCKSENATE_TRACE_FILE",
        "STOCKSENATE_OTEL_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_tracing()
    yield
    reset_tracing()


@pytest.fixture()
def offline_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("STOCKSENATE_HOME", str(home))
    monkeypatch.setenv("STOCKSENATE_LLM_PROVIDER", "offline")
    monkeypatch.setenv("STOCKSENATE_DATA_MODE", "local")
    return home


def _read_spans(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_tracing_is_off_by_default(offline_home: Path) -> None:
    assert tracing_settings(None)["enabled"] is False
    assert tracing_settings(None)["exporter"] == "none"
    assert configure_tracing(None) == "disabled"
    assert get_tracer().enabled is False

    with span("unit", {"k": "v"}) as current:
        assert isinstance(current, Span)

    assert not (offline_home / "traces").exists()


def test_a_span_nests_and_records_failure() -> None:
    recorder = _Recorder()
    tracer = Tracer(recorder)

    with tracer.span("outer", {"stage": "analysts"}) as outer:
        with tracer.span("inner", {"role": "market"}):
            pass
        with pytest.raises(ValueError):
            with tracer.span("failing"):
                raise ValueError("boom")

    assert [item.name for item in recorder.spans] == ["inner", "failing", "outer"]
    inner, failing, parent = recorder.spans
    assert inner.parent_id == parent.span_id == outer.span_id
    assert outer.parent_id is None
    assert failing.status == "error"
    assert "boom" in failing.error
    assert inner.trace_id == outer.trace_id
    assert recorder.flushes == 1


def test_jsonl_export_covers_stages_and_model_calls(offline_home: Path, tmp_path: Path) -> None:
    spans_file = tmp_path / "spans.jsonl"
    graph = SenateGraph(
        config={
            "workflow": {"tracing": {"enabled": True, "exporter": "jsonl", "path": str(spans_file)}}
        }
    )

    state, decision = graph.propagate(CODE, DAY, hitl=False)

    assert get_tracer().enabled is True
    spans = _read_spans(spans_file)
    names = {item["name"] for item in spans}
    assert STAGE_NAMES <= names
    assert any(name.startswith("llm.") for name in names)
    assert {item["trace_id"] for item in spans} == {decision["trace_id"]}

    stage_ids = {item["span_id"] for item in spans if item["name"].startswith("stage.")}
    model_spans = [item for item in spans if item["name"].startswith("llm.")]
    assert model_spans
    assert all(item["parent_id"] in stage_ids for item in model_spans)
    assert all(item["duration_ms"] >= 0 for item in spans)
    assert state["run_id"].startswith(f"{DAY}-{CODE}-")


def test_rollup_is_identical_to_a_plain_run(offline_home: Path, tmp_path: Path) -> None:
    spans_file = tmp_path / "spans.jsonl"
    no_memory = {"workflow": {"memory": {"enabled": False}}}
    traced = SenateGraph(
        config={
            "workflow": {
                "memory": {"enabled": False},
                "tracing": {
                    "enabled": True,
                    "exporter": "jsonl",
                    "path": str(spans_file),
                },
            }
        }
    )
    plain = SenateGraph(config=no_memory)

    traced_state, traced_decision = traced.propagate(CODE, DAY, hitl=False)
    plain_state, plain_decision = plain.propagate(CODE, DAY, hitl=False)

    assert traced_state["decision"]["rating"] == plain_state["decision"]["rating"]
    assert traced_state["quality"]["score"] == plain_state["quality"]["score"]
    assert traced_decision["execution"] is plain_decision["execution"] is False


def test_a_broken_exporter_never_breaks_a_run(
    offline_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    graph = SenateGraph()

    set_tracer(Tracer(_Broken()))
    with caplog.at_level(logging.WARNING, logger="stock_senate.tracing"):
        state, decision = graph.propagate(CODE, DAY, hitl=False)

    assert decision["execution"] is False
    assert state["decision"]["rating"] == decision["rating"]
    warnings = [record for record in caplog.records if "trace export failed" in record.getMessage()]
    assert len(warnings) == 1


def test_settings_env_overrides_imply_enabled(tmp_path: Path) -> None:
    config = {
        "workflow": {
            "tracing": {
                "enabled": False,
                "exporter": "none",
                "path": "ignored.jsonl",
                "endpoint": "https://config.example",
                "service_name": "configured",
                "headers_env": "CUSTOM_HEADERS",
                "timeout_seconds": 2,
            }
        }
    }

    assert tracing_settings(config)["enabled"] is False

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("STOCKSENATE_TRACE_EXPORTER", "otlp")
        patch.setenv("STOCKSENATE_TRACE_FILE", str(tmp_path / "spans.jsonl"))
        patch.setenv("STOCKSENATE_OTEL_ENDPOINT", "https://collector.example:4318")
        settings = tracing_settings(config)

    assert settings["enabled"] is True
    assert settings["exporter"] == "otlp"
    assert settings["path"] == str(tmp_path / "spans.jsonl")
    assert settings["endpoint"] == "https://collector.example:4318"
    assert settings["service_name"] == "configured"
    assert settings["headers_env"] == "CUSTOM_HEADERS"
    assert settings["timeout"] == 2.0


def test_relative_path_resolves_against_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = {
        "home": str(home),
        "workflow": {
            "tracing": {"enabled": True, "exporter": "jsonl", "path": "traces/spans.jsonl"}
        },
    }

    assert configure_tracing(config) == "jsonl"
    with span("unit"):
        pass

    assert (home / "traces" / "spans.jsonl").exists()


def test_unknown_exporter_leaves_tracing_off() -> None:
    config = {"workflow": {"tracing": {"enabled": True, "exporter": "zipkin"}}}

    assert configure_tracing(config) == "disabled"
    assert get_tracer().enabled is False


def test_otlp_payload_matches_the_collector_shape() -> None:
    calls: list[tuple[str, bytes, dict[str, str]]] = []
    exporter = OtlpExporter(
        "https://cloud.langfuse.com/api/public/otel",
        headers={"Authorization": "Bearer test"},
        service_name="stocksenate-test",
        transport=lambda url, body, headers: calls.append((url, body, dict(headers))),
    )

    with Tracer(exporter).span("stage.analysts", {"code": CODE}) as current:
        current.set_attribute("tokens", 3)

    assert len(calls) == 1
    url, body, headers = calls[0]
    assert url == "https://cloud.langfuse.com/api/public/otel/v1/traces"
    assert headers["Content-Type"] == "application/json"
    assert headers["Authorization"] == "Bearer test"

    payload = json.loads(body)
    resource = payload["resourceSpans"][0]
    attrs = {item["key"]: item["value"] for item in resource["resource"]["attributes"]}
    assert attrs["service.name"] == {"stringValue": "stocksenate-test"}
    serialized = resource["scopeSpans"][0]["spans"][0]
    assert serialized["name"] == "stage.analysts"
    assert len(serialized["traceId"]) == 32
    assert serialized["status"]["code"] == 1
    values = {item["key"]: item["value"] for item in serialized["attributes"]}
    assert values["code"] == {"stringValue": CODE}
    assert values["tokens"] == {"intValue": "3"}


def test_a_path_that_cannot_be_opened_disables_tracing(tmp_path: Path) -> None:
    blocker = tmp_path / "traces"
    blocker.write_text("not a directory", encoding="utf-8")
    config = {
        "workflow": {
            "tracing": {"enabled": True, "exporter": "jsonl", "path": str(blocker / "spans.jsonl")}
        }
    }

    assert configure_tracing(config) == "disabled"
    assert get_tracer().enabled is False


def test_jsonl_exporter_appends_one_object_per_span(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "spans.jsonl"
    tracer = Tracer(JsonlExporter(path))

    with tracer.span("first"):
        pass
    with tracer.span("second", {"stage": "risk"}):
        pass

    records = _read_spans(path)
    assert [item["name"] for item in records] == ["first", "second"]
    assert records[1]["attributes"] == {"stage": "risk"}
