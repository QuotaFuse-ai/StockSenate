"""Resilience contracts absorbed from QUOT-61.

Four behaviours that had no dedicated test:

* a weak model returning empty or unparseable output is retried locally, and a
  role that never recovers is recorded as skipped with ``partial`` set;
* every HITL outcome (approve / reject / modify / timeout, both
  ``auto_approve`` and ``abort``) leaves an auditable trace;
* token and wall-clock ceilings trip through ``Budget.charge`` and surface in
  the decision instead of crashing the run;
* report diffing is covered in ``test_report_diff.py``.

No network: the model transport is a stub and the graph runs the offline engine
over a local archive.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cli_support import CODE, DAY, write_archive, write_config_dir

from stock_senate.roles.llm import LLMUnavailable, RoleLLMResolver
from stock_senate.workflow.budget import Budget, BudgetExceeded, budget_from_config
from stock_senate.workflow.hitl import HitlReviewer, hitl_blocked


class FakeClient:
    """Answers with the next queued reply and records how often it was called."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls = 0

    def complete(self, *, system: str, user: str, json_mode: bool) -> tuple[str, dict[str, int]]:
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return reply, {"input_tokens": 10, "output_tokens": 5}


def weak_model(replies: list[str], *, budget: Budget | None = None, retries: int = 2):
    config = {
        "llm_config": {
            "llm": {
                "llm_provider": "minimax",
                "deep_think_llm": "fake-model",
                "quick_think_llm": "fake-model",
                "llm_max_retries": 0,
                "llm_retry_delay": 0,
            },
            "structured_output": {"max_local_retries": retries},
        }
    }
    resolver = RoleLLMResolver(config, budget=budget)
    client = FakeClient(replies)
    resolver._clients[("minimax", "fake-model")] = client
    return resolver, client


def call(resolver: RoleLLMResolver) -> dict[str, Any]:
    payload, _usage = resolver.complete(
        role="market", schema_name="AnalystReport", system="system", context={"code": CODE}
    )
    return payload


# --------------------------------------------------------------------------- #
# Q61-T1 weak model: empty / invalid structured output
# --------------------------------------------------------------------------- #


def test_empty_output_is_retried_and_recovers() -> None:
    budget = Budget()
    resolver, client = weak_model(["", json.dumps({"summary": "recovered"})], budget=budget)

    assert call(resolver) == {"summary": "recovered"}
    assert client.calls == 2
    assert budget.calls == 2
    assert budget.tokens == 30


def test_never_valid_output_raises_after_the_configured_retries() -> None:
    budget = Budget()
    resolver, client = weak_model(["", "not json"], retries=1, budget=budget)

    with pytest.raises(LLMUnavailable):
        call(resolver)

    assert client.calls == 2, "one initial attempt plus max_local_retries"
    assert budget.calls == 2


@pytest.fixture
def local_graph_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    archive = write_archive(tmp_path / "archive")
    config = write_config_dir(tmp_path / "config", archive)
    monkeypatch.setenv("STOCKSENATE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("STOCKSENATE_CONFIG_DIR", str(config))
    monkeypatch.setenv("STOCKSENATE_LLM_PROVIDER", "offline")
    monkeypatch.setenv("STOCKSENATE_DATA_MODE", "local")
    return config


def _resolver_failing_on(monkeypatch: pytest.MonkeyPatch, role: str, exc: Exception) -> None:
    import stock_senate.graph.senate_graph as senate_graph

    original = senate_graph._resolver

    def factory(config: dict[str, Any], budget: Budget) -> RoleLLMResolver:
        resolver = original(config, budget)
        real_complete = resolver.complete

        def complete(*, role: str, schema_name: str, system: str, context: Any):
            if role == _role:
                raise exc
            return real_complete(role=role, schema_name=schema_name, system=system, context=context)

        _role = role
        resolver.complete = complete  # type: ignore[method-assign]
        return resolver

    monkeypatch.setattr(senate_graph, "_resolver", factory)


def test_a_role_that_never_answers_is_skipped_and_marks_partial(
    local_graph_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stock_senate.graph.senate_graph import SenateGraph

    _resolver_failing_on(monkeypatch, "market", LLMUnavailable("模型返回空输出"))

    state, decision = SenateGraph(config=str(local_graph_config)).propagate(CODE, DAY, hitl=False)

    assert decision["partial"] is True
    assert state["partial"] is True
    assert [item["role"] for item in decision["skipped"]] == ["market"]
    assert decision["skipped"][0]["reason"].startswith("模型不可用")
    assert decision["rating"] != "unknown", "the remaining seats must still produce a verdict"


# --------------------------------------------------------------------------- #
# Q61-T2 HITL: approve / reject / modify / timeout
# --------------------------------------------------------------------------- #


def reviewer(handler, **hitl: Any) -> HitlReviewer:
    section = {"enabled": True, **hitl}
    return HitlReviewer({"workflow": {"hitl": section}}, handler=handler)


def test_approval_is_recorded_as_a_human_decision() -> None:
    record = reviewer(lambda checkpoint, payload: {"decision": "approved", "note": "同意"}).review(
        "after_trader", {}
    )

    assert record["decision"] == "approved"
    assert record["auto_approved"] is False
    assert record["note"] == "同意"
    assert hitl_blocked([record]) is False


def test_rejection_blocks_the_run() -> None:
    record = reviewer(
        lambda checkpoint, payload: {"decision": "rejected", "reason": "风险过高"}
    ).review("before_final", {})

    assert record["decision"] == "rejected"
    assert record["note"] == "风险过高"
    assert hitl_blocked([record]) is True


def test_a_handler_may_modify_the_verdict() -> None:
    record = reviewer(
        lambda checkpoint, payload: {"decision": "approved", "rating": "underweight"}
    ).review("before_final", {})

    assert record["rating"] == "underweight"
    assert record["decision"] == "approved"


def test_timeout_auto_approves_but_is_marked_automatic() -> None:
    def timeout(checkpoint, payload):
        raise TimeoutError("no reviewer")

    record = reviewer(timeout, on_timeout="auto_approve").review("after_trader", {})

    assert record["decision"] == "auto_approve"
    assert record["auto_approved"] is True
    assert "超时" in record["note"]
    assert hitl_blocked([record]) is False


def test_timeout_aborts_when_configured() -> None:
    def timeout(checkpoint, payload):
        raise TimeoutError("no reviewer")

    record = reviewer(timeout, on_timeout="abort").review("after_trader", {})

    assert record["decision"] == "abort"
    assert record["auto_approved"] is False
    assert hitl_blocked([record]) is True


def test_a_disabled_checkpoint_is_skipped_but_still_traced() -> None:
    record = reviewer(lambda checkpoint, payload: {"decision": "approved"}).review(
        "not_a_checkpoint", {}
    )

    assert record["decision"] == "skipped"
    assert "未启用" in record["note"]


def test_without_a_handler_the_checkpoint_auto_approves() -> None:
    record = HitlReviewer({"workflow": {"hitl": {"enabled": True}}}).review("after_trader", {})

    assert record["decision"] == "auto_approve"
    assert record["auto_approved"] is True


# --------------------------------------------------------------------------- #
# Q61-T3 budget ceilings
# --------------------------------------------------------------------------- #


def test_token_ceiling_trips_on_the_charge_that_crosses_it() -> None:
    budget = Budget(max_tokens=100)

    with pytest.raises(BudgetExceeded):
        budget.charge(input_tokens=80, output_tokens=30)

    assert budget.stopped_reason == "max_total_tokens=100"
    snapshot = budget.snapshot()
    assert snapshot["calls"] == 1
    assert snapshot["stopped_reason"] == "max_total_tokens=100"
    assert snapshot["max_tokens"] == 100


def test_wall_clock_ceiling_trips() -> None:
    budget = Budget(max_seconds=0.0)
    # Move the start forward: with a 0.0 ceiling, ``time.monotonic()`` can
    # still report an elapsed of exactly 0.0 on a coarse clock.
    budget.started -= 1.0

    with pytest.raises(BudgetExceeded):
        budget.charge()

    assert budget.stopped_reason.startswith("max_wall_clock_seconds=")


def test_call_ceiling_trips() -> None:
    budget = Budget(max_calls=1)
    budget.charge()

    with pytest.raises(BudgetExceeded):
        budget.charge()

    assert budget.stopped_reason == "max_llm_calls=1"


def test_budget_is_read_from_the_workflow_section() -> None:
    budget = budget_from_config(
        {
            "workflow": {
                "budget": {
                    "max_llm_calls": 5,
                    "max_total_tokens": 1_000,
                    "max_wall_clock_seconds": 2,
                }
            }
        }
    )

    assert (budget.max_calls, budget.max_tokens, budget.max_seconds) == (5, 1_000, 2.0)


def test_a_tripped_budget_marks_the_decisions_partial(
    local_graph_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stock_senate.graph.senate_graph import SenateGraph

    _resolver_failing_on(monkeypatch, "trader", BudgetExceeded("超过单次分析 token 上限 100"))

    state, decision = SenateGraph(config=str(local_graph_config)).propagate(CODE, DAY, hitl=False)

    assert decision["partial"] is True
    assert state["partial"] is True
    assert decision["budget_tripped"] == "超过单次分析 token 上限 100"
    assert decision["execution"] is False
