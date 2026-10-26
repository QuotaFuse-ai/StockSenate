"""Quality gate modes: rules, rubric judge and the hybrid blend.

The gate must never be the step that loses a finished analysis. These tests
pin the contract:

* the default mode is deterministic rules and makes no model call;
* ``judge`` scores from the model's rubric and keeps its located defects;
* structural blockers found by the rules cannot be outvoted by a generous
  judge, and a compliance finding always forces ``low_quality``;
* prohibited wording in the report body is found by the rules even when the
  judge reports none, and ``hybrid`` caps how far the judge may outrun them;
* ``hybrid`` blends the two scores with the configured weight;
* every way the judge can fail - offline provider, unusable object, payload
  that does not validate - falls back to the rules verdict with a note.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from stock_senate.config import schema
from stock_senate.roles.registry import RoleRegistry, RoleSpec
from stock_senate.roles.schemas import (
    AnalystReport,
    Citation,
    FinalDecision,
    ResearchVerdict,
    RiskArgument,
    TradePlan,
)
from stock_senate.workflow import judge
from stock_senate.workflow.budget import Budget
from stock_senate.workflow.context import RunContext
from stock_senate.workflow.nodes.quality import run_quality_gate

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
CODE = "600519"
DAY = "2026-10-26"

GOOD_DIGEST = {
    "domains": {
        "price": {"status": "ok", "facts": [{"label": "收盘价", "value": "1680.0"}]},
        "news": {"status": "ok", "facts": [{"label": "公告", "value": "三季报"}]},
    },
    "missing": [],
}

THIN_DIGEST = {
    "domains": {"price": {"status": "ok", "facts": []}},
    "missing": [],
}


class StubResolver:
    """A resolver whose judge answer is chosen by the test."""

    def __init__(self, payload: Any = None, *, provider: str = "anthropic") -> None:
        self.payload = payload if payload is not None else _judge_payload()
        self.provider = provider
        self.calls: list[dict[str, Any]] = []

    def target(self, role: str) -> SimpleNamespace:
        return SimpleNamespace(role=role, provider=self.provider, model="stub-judge")

    def complete(
        self, *, role: str, schema_name: str, system: str, context: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        self.calls.append(
            {"role": role, "schema_name": schema_name, "system": system, "context": context}
        )
        return self.payload, {"provider": self.provider, "model": "stub-judge"}


def _judge_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "role": "quality_gate",
        "score": 92.0,
        "low_quality": False,
        "threshold": 70.0,
        "dimensions": {
            "logic_coherence": {"score": 95.0, "note": "链条完整"},
            "evidence_sufficiency": {"score": 90.0, "note": "证据可追溯"},
            "citation_accuracy": {"score": 91.0, "note": "引用齐全"},
        },
        "defects": [
            {
                "dimension": "citation_accuracy",
                "location": "研究经理结论",
                "issue": "有一处引用缺少年月日",
                "fix_hint": "补上 data_as_of",
            }
        ],
        "compliance": {"price_levels_found": False, "disclaimer_present": True},
        "notes": "整体可用。",
    }
    payload.update(overrides)
    return payload


def _config(mode: str | None = "rules", *, weight: float = 0.5, threshold: float = 70) -> dict:
    gate: dict[str, Any] = {"enabled": True, "threshold": threshold}
    if mode is not None:
        gate["mode"] = mode
    gate["judge_weight"] = weight
    return {"workflow": {"quality_gate": gate}}


def _ctx(config: dict, resolver: StubResolver, digest: dict | None = None) -> RunContext:
    spec = RoleSpec(
        key="quality_gate",
        display_name="质量闸门",
        kind="manager",
        prompt="",
        tools=(),
        output_schema="QualityVerdict",
    )
    return RunContext(
        code=CODE,
        date=DAY,
        config=config,
        registry=RoleRegistry({"quality_gate": spec}),
        gate=None,  # type: ignore[arg-type]
        resolver=resolver,  # type: ignore[arg-type]
        data=None,  # type: ignore[arg-type]
        envelopes={},
        digest=GOOD_DIGEST if digest is None else digest,
        budget=Budget(),
        trace_id="test-trace",
        home=Path("."),
    )


def _analysts() -> list[AnalystReport]:
    return [
        AnalystReport(
            role="technical",
            display_name="技术面",
            citations=[Citation(domain="price", source="fixture", data_as_of=DAY)],
        )
    ]


def _run(
    mode: str | None = "rules",
    *,
    payload: Any = None,
    provider: str = "anthropic",
    digest: dict | None = None,
    weight: float = 0.5,
    threshold: float = 70,
    decision: FinalDecision | None = None,
) -> tuple[Any, StubResolver, RunContext]:
    resolver = StubResolver(payload, provider=provider)
    ctx = _ctx(_config(mode, weight=weight, threshold=threshold), resolver, digest)
    result = run_quality_gate(
        ctx,
        _analysts(),
        ResearchVerdict(role="research_manager", stance="bullish", rationale=["证据一致"]),
        TradePlan(role="trader", direction="long", rationale=["趋势确认"]),
        [RiskArgument(role="risk_neutral", side="neutral", points=["波动率偏高"])],
        decision,
    )
    return result, resolver, ctx


def _rules_score(digest: dict | None = None) -> float:
    return judge.rule_verdict(
        {
            "digest": GOOD_DIGEST if digest is None else digest,
            "analysts": [report.model_dump() for report in _analysts()],
            "verdict": {"rationale": ["证据一致"]},
            "trade": {"rationale": ["趋势确认"]},
            "risks": [{"points": ["波动率偏高"]}],
            "threshold": 70,
        }
    ).score


def test_default_mode_is_rules_and_never_calls_a_model() -> None:
    result, resolver, _ctx_ = _run(None)

    assert result.mode == "rules"
    assert result.score == _rules_score() == 100.0
    assert result.low_quality is False
    assert resolver.calls == []


def test_judge_mode_uses_the_rubric_and_keeps_located_defects() -> None:
    result, resolver, _ctx_ = _run("judge")

    assert result.mode == "judge"
    assert result.score == 92.0
    assert result.low_quality is False
    assert set(result.dimensions) == set(judge.RUBRIC_DIMENSIONS)
    assert result.dimensions["logic_coherence"].score == 95.0
    assert len(result.defects) == 1
    assert result.defects[0].location == "研究经理结论"
    assert result.compliance["disclaimer_present"] is True

    assert len(resolver.calls) == 1
    call = resolver.calls[0]
    assert call["role"] == "quality_gate"
    assert call["schema_name"] == "QualityVerdict"
    assert call["context"]["digest"] == GOOD_DIGEST
    assert call["context"]["threshold"] == 70


def test_judge_mode_cannot_lift_a_rules_blocker() -> None:
    result, _resolver, _ctx_ = _run("judge", digest=THIN_DIGEST)

    assert result.score == 92.0
    assert result.partial is True
    assert result.low_quality is True
    assert "存在无事实的数据域" in result.notes


def test_judge_mode_flags_executable_prices() -> None:
    result, _resolver, _ctx_ = _run(
        "judge",
        payload=_judge_payload(compliance={"price_levels_found": True, "disclaimer_present": True}),
    )

    assert result.low_quality is True
    assert "可执行价位" in result.notes


def test_deterministic_scan_reads_the_report_text_the_judge_scores() -> None:
    hits = judge.deterministic_compliance(
        {
            "analysts": [
                {
                    "summary": "机构给出的目标价为 12 元。",
                    "facts": [{"label": "备注", "value": "止损位 9 元"}],
                }
            ],
            "decision": {"reasons": ["估值合理。"], "risks": ["仓位建议 30%。"]},
        }
    )

    assert set(hits) == {"目标价", "止损位", "仓位建议"}
    assert judge.deterministic_compliance(None) == []


def test_judge_mode_scans_the_report_when_the_judge_misses_a_price() -> None:
    result, _resolver, _ctx_ = _run(
        "judge",
        decision=FinalDecision(
            role="portfolio_manager",
            rating="hold",
            reasons=["目标价 12.00 元。"],
            risks=["行业需求仍有波动。"],
        ),
    )

    assert result.low_quality is True
    assert result.compliance["price_levels_found"] is True
    assert "报告正文" in result.notes
    assert "目标价" in result.notes


def test_hybrid_mode_scans_the_report_when_the_judge_misses_a_price() -> None:
    result, _resolver, _ctx_ = _run(
        "hybrid",
        weight=0.85,
        decision=FinalDecision(
            role="portfolio_manager",
            rating="hold",
            reasons=["营收增速与现金流匹配。"],
            risks=["止损位 9.50 元。"],
        ),
    )

    # Without the deterministic scan this blend would score 93.2 and pass.
    assert result.score > 90
    assert result.low_quality is True
    assert result.compliance["price_levels_found"] is True
    assert "止损位" in result.notes


def test_hybrid_caps_an_inflated_judge_at_the_rules_anchor() -> None:
    result, _resolver, _ctx_ = _run(
        "hybrid",
        payload=_judge_payload(
            score=100.0,
            dimensions={
                "logic_coherence": {"score": 100.0, "note": "链条完整"},
                "evidence_sufficiency": {"score": 100.0, "note": "证据充分"},
                "citation_accuracy": {"score": 100.0, "note": "引用齐全"},
            },
        ),
        digest=THIN_DIGEST,
        weight=0.5,
    )

    # Rules score 60.0, so the judge may count at most 85.0: (60 + 85) / 2.
    assert result.score == 72.5
    assert result.dimensions["evidence_sufficiency"].score == 42.5
    assert "允许上浮" in result.notes
    assert result.low_quality is True


def test_hybrid_mode_blends_scores_with_the_configured_weight() -> None:
    result, _resolver, _ctx_ = _run(
        "hybrid",
        payload=_judge_payload(
            score=60.0,
            dimensions={
                "logic_coherence": {"score": 60.0, "note": "链条有断点"},
                "evidence_sufficiency": {"score": 60.0, "note": "证据偏薄"},
                "citation_accuracy": {"score": 60.0, "note": "引用不全"},
            },
        ),
        weight=0.25,
        threshold=70,
    )

    assert result.mode == "hybrid"
    assert result.score == 90.0
    assert result.dimensions["logic_coherence"].score == 90.0
    assert result.low_quality is False
    assert "规则分 100.0 与评审分 60.0 按 25% 权重合并为 90.0" in result.notes
    assert len(result.defects) == 1


def test_offline_provider_falls_back_to_rules_with_a_note() -> None:
    result, resolver, _ctx_ = _run("judge", provider="offline")

    assert resolver.calls == []
    assert result.mode == "rules"
    assert result.score == 100.0
    assert "offline" in result.notes


def test_unusable_judge_falls_back_to_rules() -> None:
    result, _resolver, ctx = _run(
        "judge",
        payload={"role": "quality_gate", "score": 88.0, "dimensions": {}},
    )

    assert result.mode == "rules"
    assert result.score == 100.0
    assert "评审模型未给出可用评分" in result.notes
    assert ctx.skipped == []


def test_invalid_judge_payload_is_recorded_and_falls_back() -> None:
    result, _resolver, ctx = _run("judge", payload=["not", "an", "object"])

    assert result.mode == "rules"
    assert result.score == 100.0
    assert "评审模型未给出可用评分" in result.notes
    assert ctx.skipped == [{"role": "quality_gate", "reason": "结构化输出校验失败"}]


def test_configured_threshold_flows_into_the_verdict() -> None:
    result, _resolver, _ctx_ = _run("judge", threshold=95)

    assert result.threshold == 95
    assert result.low_quality is True


def test_normalize_helpers_never_raise() -> None:
    assert judge.normalize_mode(" HYBRID ") == "hybrid"
    assert judge.normalize_mode("llm") == "rules"
    assert judge.normalize_mode(None) == "rules"

    assert judge.normalize_weight(0.3) == 0.3
    assert judge.normalize_weight("0.8") == 0.8
    assert judge.normalize_weight(-1) == 0.0
    assert judge.normalize_weight(3) == 1.0
    assert judge.normalize_weight("abc") == judge.DEFAULT_JUDGE_WEIGHT
    assert judge.normalize_weight(None) == judge.DEFAULT_JUDGE_WEIGHT


def _copy_config(tmp_path: Path) -> Path:
    target = tmp_path / "config"
    shutil.copytree(CONFIG, target)
    return target


@pytest.mark.parametrize("mode", ["rules", "judge", "hybrid"])
def test_config_accepts_every_gate_mode(tmp_path: Path, mode: str) -> None:
    directory = _copy_config(tmp_path)
    path = directory / "workflow.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace('mode = "rules"', f'mode = "{mode}"', 1),
        encoding="utf-8",
    )

    report = schema.validate_dir(directory, strict=True)

    assert report["ok"] is True, report["errors"]


def test_config_rejects_an_unknown_mode_and_an_out_of_range_weight(tmp_path: Path) -> None:
    directory = _copy_config(tmp_path)
    path = directory / "workflow.toml"
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace('mode = "rules"', 'mode = "llm"', 1)
        .replace("judge_weight = 0.5", "judge_weight = 1.5", 1),
        encoding="utf-8",
    )

    report = schema.validate_dir(directory)

    assert report["ok"] is False
    keys = {error["key"] for error in report["errors"]}
    assert "quality_gate.mode" in keys
    assert "quality_gate.judge_weight" in keys
