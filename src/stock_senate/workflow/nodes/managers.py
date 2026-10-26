"""Manager stages: research manager, trader and portfolio manager."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from stock_senate.roles.schemas import (
    AnalystReport,
    FinalDecision,
    ResearchVerdict,
    RiskArgument,
    TradePlan,
)
from stock_senate.workflow.context import RunContext
from stock_senate.workflow.nodes.base import run_role
from stock_senate.workflow.nodes.debate_nodes import DebateResult


def _fallback_verdict(reason: str) -> ResearchVerdict:
    return ResearchVerdict(
        role="research_manager",
        partial=True,
        stance="unknown",
        confidence=0.0,
        rationale=[reason[:300]],
        unresolved=[reason[:200]],
        notes="研究结论不可用",
    )


def _fallback_trade(reason: str) -> TradePlan:
    return TradePlan(
        role="trader",
        partial=True,
        direction="unknown",
        horizon="unknown",
        rationale=[reason[:300]],
        risks=[],
        confidence=0.0,
        notes="交易倾向不可用",
    )


def _fallback_decision(reason: str, hitl: Mapping[str, Any]) -> FinalDecision:
    return FinalDecision(
        role="portfolio_manager",
        partial=True,
        rating="unknown",
        horizon="unknown",
        reasons=[reason[:300]],
        risks=[],
        confidence=0.0,
        low_quality=True,
        hitl=dict(hitl),
        notes="终审结论不可用",
    )


def _reports(analysts: Sequence[AnalystReport]) -> list[dict[str, Any]]:
    return [report.model_dump() for report in analysts]


def run_research_manager(
    ctx: RunContext,
    analysts: Sequence[AnalystReport],
    debate: DebateResult | None = None,
) -> ResearchVerdict:
    context: dict[str, Any] = {
        "code": ctx.code,
        "date": ctx.date,
        "role": "research_manager",
        "analysts": _reports(analysts),
        "weights": ctx.weights,
        "digest": ctx.digest,
        "rounds_used": debate.rounds_used if debate else 0,
        "convergence_reason": debate.convergence_reason if debate else "",
        "debate": debate.as_dict() if debate else {},
    }
    if debate:
        by_side = debate.by_side
        context["bull_case"] = by_side["bull"].model_dump() if "bull" in by_side else {}
        context["bear_case"] = by_side["bear"].model_dump() if "bear" in by_side else {}

    model = run_role(ctx, "research_manager", context)
    if isinstance(model, ResearchVerdict):
        return model
    ctx.skipped.append({"role": "research_manager", "reason": "Schema 不是 ResearchVerdict"})
    return _fallback_verdict("研究经理输出 Schema 校验失败")


def run_trader(
    ctx: RunContext,
    verdict: ResearchVerdict,
    analysts: Sequence[AnalystReport] | None = None,
) -> TradePlan:
    context = {
        "code": ctx.code,
        "date": ctx.date,
        "role": "trader",
        "verdict": verdict.model_dump(),
        "analysts": _reports(analysts or []),
        "digest": ctx.digest,
    }
    model = run_role(ctx, "trader", context)
    if isinstance(model, TradePlan):
        return model
    ctx.skipped.append({"role": "trader", "reason": "Schema 不是 TradePlan"})
    return _fallback_trade("交易员输出 Schema 校验失败")


def run_portfolio_manager(
    ctx: RunContext,
    verdict: ResearchVerdict,
    trade: TradePlan,
    risks: Sequence[RiskArgument],
    hitl: Mapping[str, Any] | None = None,
) -> FinalDecision:
    hitl_payload = dict(hitl or {})
    context = {
        "code": ctx.code,
        "date": ctx.date,
        "role": "portfolio_manager",
        "verdict": verdict.model_dump(),
        "trade": trade.model_dump(),
        "risks": [argument.model_dump() for argument in risks],
        "digest": ctx.digest,
        "hitl": hitl_payload,
    }
    model = run_role(ctx, "portfolio_manager", context)
    if not isinstance(model, FinalDecision):
        ctx.skipped.append({"role": "portfolio_manager", "reason": "Schema 不是 FinalDecision"})
        return _fallback_decision("投资经理输出 Schema 校验失败", hitl_payload)
    if hitl_payload and not model.hitl:
        model = model.model_copy(update={"hitl": hitl_payload})
    return model


__all__ = ["run_portfolio_manager", "run_research_manager", "run_trader"]
