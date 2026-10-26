"""Quality gate node.

The gate scores the finished chain on evidence coverage, citation validity and
chain completeness. ``workflow.quality_gate.mode`` decides who scores it: the
deterministic rules (default, no model call), an independent rubric judge, or
a weighted blend of both. A model-driven mode falls back to the rules verdict
when the judge is unreachable, so the gate can never fail a finished run.

It never blocks by itself: a score below the configured threshold marks the
decision ``low_quality`` so the report and the CLI exit code can surface it,
while the decision itself still reaches the reader.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from stock_senate.roles.schemas import (
    AnalystReport,
    FinalDecision,
    QualityVerdict,
    ResearchVerdict,
    RiskArgument,
    TradePlan,
)
from stock_senate.workflow import judge as judge_module
from stock_senate.workflow.context import RunContext
from stock_senate.workflow.nodes.base import run_role


def _gate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    return judge_module.gate_settings(config)


def _fallback(reason: str, threshold: float) -> QualityVerdict:
    return QualityVerdict(
        role="quality_gate",
        partial=True,
        score=0.0,
        low_quality=True,
        threshold=threshold,
        dimensions={},
        notes=f"质量评估不可用：{reason}"[:300],
    )


def _with_note(verdict: QualityVerdict, note: str) -> QualityVerdict:
    notes = f"{verdict.notes} {note}".strip()
    return verdict.model_copy(update={"notes": notes[:500]})


def run_quality_gate(
    ctx: RunContext,
    analysts: Sequence[AnalystReport],
    verdict: ResearchVerdict,
    trade: TradePlan,
    risks: Sequence[RiskArgument],
    decision: FinalDecision | None = None,
) -> QualityVerdict:
    settings = _gate_config(ctx.config)
    threshold = float(settings.get("threshold", 70))
    mode = judge_module.normalize_mode(settings.get("mode"))

    context: dict[str, Any] = {
        "code": ctx.code,
        "date": ctx.date,
        "role": "quality_gate",
        "digest": ctx.digest,
        "analysts": [report.model_dump() for report in analysts],
        "verdict": verdict.model_dump(),
        "trade": trade.model_dump(),
        "risks": [argument.model_dump() for argument in risks],
        "threshold": threshold,
    }
    if decision is not None:
        context["decision"] = decision.model_dump()

    rules = judge_module.rule_verdict(context)
    if mode == "rules":
        return rules

    if judge_module.judge_provider(ctx.resolver) == "offline":
        return _with_note(rules, "当前模型配置为 offline，没有独立评审模型，按规则评分。")

    model = run_role(ctx, "quality_gate", context)
    if not isinstance(model, QualityVerdict):
        ctx.skipped.append({"role": "quality_gate", "reason": "Schema 不是 QualityVerdict"})
        return _with_note(_fallback("质量闸门输出 Schema 校验失败", threshold), "已回退规则评分。")
    if not judge_module.judge_usable(model):
        return _with_note(rules, "评审模型未给出可用评分，已回退规则评分。")

    if mode == "judge":
        return judge_module.finalize_judge(model, rules, threshold=threshold)
    return judge_module.combine(
        rules,
        model,
        weight=judge_module.normalize_weight(settings.get("judge_weight")),
        threshold=threshold,
    )


__all__ = ["run_quality_gate"]
