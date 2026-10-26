"""Context compression: deterministic, citation-preserving, offline-safe."""

from __future__ import annotations

import json
import logging

import pytest

from stock_senate.roles.llm import RoleLLMResolver
from stock_senate.workflow.compress import (
    compress_context,
    estimate_tokens,
    settings_from_config,
)

AS_OF = "2026-01-05"


def _digest(domains: int = 6, facts: int = 6) -> dict:
    payload: dict[str, dict] = {}
    for index in range(domains):
        payload[f"domain{index}"] = {
            "status": "ok",
            "source": "fixture",
            "data_as_of": AS_OF,
            "facts": [f"域{index}事实{number}数值 {index}{number}" for number in range(facts)],
            "citations": [f"https://example.com/{index}/{number}" for number in range(3)],
            "items": [{"title": "长文本" * 400, "summary": "描述" * 400}],
        }
    return {"digest": payload, "code": "600519"}


def _tokens(value) -> int:
    return estimate_tokens(json.dumps(value, ensure_ascii=False, default=str))


def test_token_estimate_is_positive_and_monotone() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("中文") == 2
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("中文abcdefgh") == 4


def test_settings_are_read_from_the_workflow_section() -> None:
    config = {"workflow": {"compression": {"trigger_tokens": 100}}}
    assert settings_from_config(config) == {"trigger_tokens": 100}
    assert settings_from_config({}) == {}


def test_context_below_trigger_is_returned_unchanged() -> None:
    context = {"code": "600519", "digest": {"news": {"facts": ["一条事实"]}}}
    payload, record = compress_context(context, {"trigger_tokens": 100000})
    assert payload == context
    assert record.triggered is False
    assert record.original_tokens == record.compressed_tokens


def test_disabled_compression_never_rewrites_the_context() -> None:
    context = _digest()
    payload, record = compress_context(context, {"enabled": False, "trigger_tokens": 1})
    assert payload == context
    assert record.enabled is False
    assert record.triggered is False


def test_oversized_context_is_reduced_and_keeps_citations() -> None:
    context = _digest()
    original = _tokens(context)
    payload, record = compress_context(
        context, {"trigger_tokens": 200, "chunk_tokens": 120, "keep_citations": True}
    )
    assert record.triggered is True
    assert record.original_tokens >= original
    assert record.compressed_tokens < record.original_tokens
    assert record.citations == 18

    text = json.dumps(payload, ensure_ascii=False)
    for index in range(6):
        for number in range(3):
            assert f"https://example.com/{index}/{number}" in text
    assert "域0事实0数值 00" in text
    assert payload["code"] == "600519"


def test_compression_is_deterministic() -> None:
    context = _digest()
    settings = {"trigger_tokens": 200, "chunk_tokens": 120}
    first, first_record = compress_context(context, settings)
    second, second_record = compress_context(context, settings)
    assert first == second
    assert first_record.as_dict() == second_record.as_dict()


def test_chunking_splits_list_evidence_without_losing_items() -> None:
    context = {"analysts": [{"role": f"role{index}", "body": "长文" * 300} for index in range(8)]}
    payload, record = compress_context(context, {"trigger_tokens": 100, "chunk_tokens": 200})
    assert record.triggered is True
    assert record.chunks > 1
    assert len(payload["analysts"]) == 8
    assert record.compressed_tokens < record.original_tokens


def test_citations_can_be_dropped_when_configured() -> None:
    context = _digest(domains=1, facts=1)
    payload, _record = compress_context(
        context,
        {"trigger_tokens": 50, "chunk_tokens": 40, "keep_citations": False},
    )
    text = json.dumps(payload, ensure_ascii=False)
    assert "https://example.com/0/0" not in text


def test_the_assembled_payload_gets_its_own_ceiling() -> None:
    context = _digest(domains=8, facts=6)
    settings = {"trigger_tokens": 200, "chunk_tokens": 8000, "max_output_tokens": 2000}
    payload, record = compress_context(context, settings)
    assert record.triggered is True
    assert record.capped is False
    assert record.compressed_tokens <= 2000
    assert _tokens(payload) <= 2000


def test_citations_alone_can_hold_the_payload_over_the_ceiling(
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = {
        "digest": {
            "d": {
                "citations": [f"https://example.com/{index}" for index in range(400)],
                "facts": ["一条事实"],
            }
        }
    }

    with caplog.at_level(logging.WARNING, logger="stock_senate.compression"):
        payload, record = compress_context(
            context,
            {"trigger_tokens": 10, "chunk_tokens": 5, "max_output_tokens": 60},
        )

    assert record.triggered is True
    # Keeping citations verbatim is deliberate, so the record says the ceiling
    # could not be met instead of silently dropping the audit trail.
    assert record.capped is True
    assert _tokens(payload) > 60
    assert any("max_output_tokens" in item.getMessage() for item in caplog.records)


def test_dropping_citations_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="stock_senate.compression"):
        compress_context(
            _digest(domains=1, facts=1),
            {"trigger_tokens": 50, "chunk_tokens": 40, "keep_citations": False},
        )

    assert any("citation" in item.getMessage() for item in caplog.records)


def test_resolver_prompt_reports_compression_without_touching_offline() -> None:
    config = {
        "llm_config": {"llm": {"llm_provider": "offline"}},
        "workflow": {"compression": {"trigger_tokens": 200, "chunk_tokens": 120}},
    }
    resolver = RoleLLMResolver(config)
    prompt, record = resolver._prompt("AnalystReport", _digest())
    assert record["triggered"] is True
    assert record["compressed_tokens"] < record["original_tokens"]
    assert "https://example.com/0/0" in prompt

    payload, usage = resolver.complete(
        role="market_analyst",
        schema_name="AnalystReport",
        system="s",
        context={"role": "market_analyst", "digest": _digest()["digest"]},
    )
    assert payload["role"] == "market_analyst"
    assert usage["provider"] == "offline"
    assert "compression" not in usage
    assert resolver.calls[-1]["provider"] == "offline"
