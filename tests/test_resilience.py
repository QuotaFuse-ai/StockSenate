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

import io
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from cli_support import CODE, DAY, cli_env, parse_json, run_cli, write_archive, write_config_dir

from stock_senate.roles.llm import LLMUnavailable, RoleLLMResolver
from stock_senate.workflow.budget import Budget, BudgetExceeded, budget_from_config
from stock_senate.workflow.hitl import HitlReviewer, hitl_blocked, interactive_handler


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


# --------------------------------------------------------------------------- #
# QUOT-49 P0-1: the reviewer is wired into the graph, the deadline is real
# --------------------------------------------------------------------------- #


def test_a_stalled_handler_is_cut_off_at_the_configured_deadline() -> None:
    def stall(checkpoint, payload):
        time.sleep(10)
        return {"decision": "approved"}

    started = time.monotonic()
    record = reviewer(stall, timeout_seconds=0.05).review("after_trader", {})

    assert time.monotonic() - started < 2.0, "a stalled reviewer must not hold the run"
    assert record["decision"] == "auto_approve"
    assert record["auto_approved"] is True
    assert "0.05s" in record["note"]


def test_a_stalled_handler_aborts_when_on_timeout_says_so() -> None:
    def stall(checkpoint, payload):
        time.sleep(10)

    record = reviewer(stall, timeout_seconds=0.05, on_timeout="abort").review("before_final", {})

    assert record["decision"] == "abort"
    assert record["auto_approved"] is False
    assert "0.05s" in record["note"]
    assert hitl_blocked([record]) is True


def test_ctrl_c_during_the_wait_counts_as_no_answer() -> None:
    class Interrupted(threading.Event):
        def wait(self, timeout=None):
            raise KeyboardInterrupt

    with pytest.raises(TimeoutError):
        reviewer(lambda checkpoint, payload: {"decision": "approved"})._await(Interrupted())


def test_a_non_positive_deadline_leaves_the_handler_unsupervised() -> None:
    def approve(checkpoint, payload):
        return {"decision": "approved"}

    record = reviewer(approve, timeout_seconds=0).review("after_trader", {})

    assert record["decision"] == "approved"
    assert record["auto_approved"] is False


def test_propagate_asks_the_supplied_handler_at_each_checkpoint(
    local_graph_config: Path,
) -> None:
    from stock_senate.graph.senate_graph import SenateGraph

    asked: list[tuple[str, str]] = []

    def handler(checkpoint: str, payload: Any) -> dict[str, Any]:
        asked.append((checkpoint, str(payload.get("code"))))
        return {"decision": "approved", "note": "人工同意"}

    state, decision = SenateGraph(config=str(local_graph_config)).propagate(
        CODE, DAY, hitl=True, hitl_handler=handler
    )

    assert [item[0] for item in asked] == ["after_trader", "before_final"]
    assert {item[1] for item in asked} == {CODE}
    traces = state["hitl"]["traces"]
    assert [trace["decision"] for trace in traces] == ["approved", "approved"]
    assert [trace["auto_approved"] for trace in traces] == [False, False]
    assert [trace["note"] for trace in traces] == ["人工同意", "人工同意"]
    assert decision["hitl"]["traces"] == traces


def test_a_rejection_reaches_the_decision_as_partial(local_graph_config: Path) -> None:
    from stock_senate.graph.senate_graph import SenateGraph

    def handler(checkpoint: str, payload: Any) -> dict[str, Any]:
        if checkpoint == "before_final":
            return {"decision": "rejected", "note": "风险过高"}
        return {"decision": "approved"}

    state, decision = SenateGraph(config=str(local_graph_config)).propagate(
        CODE, DAY, hitl=True, hitl_handler=handler
    )

    assert [trace["decision"] for trace in decision["hitl"]["traces"]] == [
        "approved",
        "rejected",
    ]
    assert decision["partial"] is True
    assert state["partial"] is True


def _scripted_input(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    pending = list(lines)

    def fake_input(*_args: Any, **_kwargs: Any) -> str:
        if not pending:
            raise EOFError
        return pending.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        ([""], {"decision": "approved"}),
        (["a"], {"decision": "approved"}),
        (["r"], {"decision": "rejected", "note": "人工驳回"}),
        (["q"], {"decision": "abort", "note": "人工中止"}),
        (
            ["m", "rating=hold direction=hold"],
            {
                "decision": "approved",
                "note": "人工修改",
                "rating": "hold",
                "direction": "hold",
            },
        ),
        (["what", "a"], {"decision": "approved"}),
    ],
)
def test_interactive_handler_turns_keystrokes_into_a_verdict(
    monkeypatch: pytest.MonkeyPatch, answers: list[str], expected: dict[str, Any]
) -> None:
    _scripted_input(monkeypatch, answers)
    handler = interactive_handler(out=io.StringIO())

    answer = handler(
        "after_trader",
        {
            "code": CODE,
            "date": DAY,
            "research": {"stance": "bull"},
            "trade": {"direction": "buy"},
        },
    )

    assert dict(answer) == expected


def test_interactive_handler_treats_eof_as_no_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    _scripted_input(monkeypatch, [])
    handler = interactive_handler(out=io.StringIO())

    with pytest.raises(TimeoutError):
        handler("after_trader", {})


def test_interactive_handler_gives_up_after_three_invalid_choices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scripted_input(monkeypatch, ["x", "z", "w"])
    handler = interactive_handler(out=io.StringIO())

    with pytest.raises(TimeoutError):
        handler("after_trader", {})


def test_cli_without_a_tty_keeps_the_checkpoints_non_blocking(tmp_path: Path) -> None:
    archive = write_archive(tmp_path / "archive")
    config = write_config_dir(tmp_path / "config", archive)

    result = run_cli(
        "analyze",
        CODE,
        "--date",
        DAY,
        "--output",
        "json",
        env=cli_env(home=tmp_path / "home", config_dir=config),
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    traces = parse_json(result)["decision"]["hitl"]["traces"]
    assert [trace["decision"] for trace in traces] == ["auto_approve", "auto_approve"]
    assert all("未配置人工处理器" in trace["note"] for trace in traces)
    assert "[人工复核]" not in result.stderr


# --------------------------------------------------------------------------- #
# QUOT-49 P0-2: budget.per_run.on_exceed is consumed at runtime
# --------------------------------------------------------------------------- #


def test_on_exceed_is_read_from_budget_toml_and_snapshotted() -> None:
    budget = budget_from_config({"budget": {"per_run": {"on_exceed": " Fail "}}})

    assert budget.on_exceed == "fail"
    assert budget.snapshot()["on_exceed"] == "fail"
    assert budget_from_config({}).on_exceed == "stop"


def _write_on_exceed(config_dir: Path, value: str) -> None:
    (config_dir / "budget.toml").write_text(f'[per_run]\non_exceed = "{value}"\n', encoding="utf-8")


def test_on_exceed_fail_raises_instead_of_degrading(
    local_graph_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stock_senate.graph.senate_graph import SenateGraph

    _write_on_exceed(local_graph_config, "fail")
    _resolver_failing_on(monkeypatch, "trader", BudgetExceeded("超过单次分析 token 上限 100"))

    graph = SenateGraph(config=str(local_graph_config))

    with pytest.raises(BudgetExceeded, match="on_exceed=fail"):
        graph.propagate(CODE, DAY, hitl=False)


def test_on_exceed_stop_keeps_the_degraded_decision(
    local_graph_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stock_senate.graph.senate_graph import SenateGraph

    _write_on_exceed(local_graph_config, "stop")
    _resolver_failing_on(monkeypatch, "trader", BudgetExceeded("超过单次分析 token 上限 100"))

    state, decision = SenateGraph(config=str(local_graph_config)).propagate(CODE, DAY, hitl=False)

    assert decision["partial"] is True
    assert decision["budget_tripped"].startswith("超过单次分析")
    assert state["budget"]["on_exceed"] == "stop"
