"""Quality gate node.

The gate scores the finished chain on evidence coverage, citation validity and
chain completeness. It never blocks by itself: a score below the configured
threshold marks the decision ``low_quality`` so the report and the CLI exit
code can surface it, while the decision itself still reaches the reader.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from stock_senate.roles.schemas import (
    AnalystReport,
    FinalDecision,
    QualityVerdict,
    ResearchVerdict,
    RiskArgument,
    TradePlan,
)
from stock_senate.workflow.context import RunContext
from stock_senate.workflow.nodes.base import run_role


def _gate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    return dict(((config or {}).get("workflow") or {}).get("quality_gate") or {})


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

    model = run_role(ctx, "quality_gate", context)
    if not isinstance(model, QualityVerdict):
        ctx.skipped.append({"role": "quality_gate", "reason": "Schema 不是 QualityVerdict"})
        return _fallback("质量闸门输出 Schema 校验失败", threshold)
    return model.model_copy(
        update={
            "threshold": threshold,
            "low_quality": model.score < threshold,
        }
    )


__all__ = ["run_quality_gate"]
