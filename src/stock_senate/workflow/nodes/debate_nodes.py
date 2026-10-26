"""Debate stages: bull vs bear, then the three-tier risk discussion.

Each round every side speaks once. The debate ends early only when all sides
have stopped producing new points (see ``workflow.debate``); otherwise it runs
to the configured round limit. The final round's arguments plus the round count
and stop reason are what the next role receives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from stock_senate.roles.schemas import (
    AnalystReport,
    ResearchArgument,
    ResearchVerdict,
    RiskArgument,
    TradePlan,
)
from stock_senate.workflow.context import RunContext
from stock_senate.workflow.debate import DebateSettings, converged
from stock_senate.workflow.nodes.base import run_role

RISK_SIDES = ("aggressive", "neutral", "conservative")


@dataclass
class DebateResult:
    arguments: list[ResearchArgument]
    rounds_used: int
    convergence_reason: str
    new_point_ratio: float
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def by_side(self) -> dict[str, ResearchArgument]:
        return {argument.side: argument for argument in self.arguments}

    def as_dict(self) -> dict[str, Any]:
        return {
            "rounds_used": self.rounds_used,
            "convergence_reason": self.convergence_reason,
            "new_point_ratio": round(self.new_point_ratio, 4),
            "arguments": [argument.model_dump() for argument in self.arguments],
            "history": self.history,
        }


@dataclass
class RiskDebateResult:
    arguments: list[RiskArgument]
    rounds_used: int
    convergence_reason: str
    new_point_ratio: float
    history: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rounds_used": self.rounds_used,
            "convergence_reason": self.convergence_reason,
            "new_point_ratio": round(self.new_point_ratio, 4),
            "arguments": [argument.model_dump() for argument in self.arguments],
            "history": self.history,
        }


def _fallback_argument(side: str, reason: str) -> ResearchArgument:
    return ResearchArgument(
        role=side,
        side=side,  # type: ignore[arg-type]
        points=[],
        rebuttals=[],
        confidence=0.0,
        partial=True,
        notes=f"辩论角色输出不可用：{reason}"[:500],
    )


def _fallback_risk(side: str, reason: str) -> RiskArgument:
    return RiskArgument(
        role=f"risk_{side}",
        side=side,  # type: ignore[arg-type]
        stance="unknown",
        points=[],
        severity="unknown",
        partial=True,
        notes=f"风险角色输出不可用：{reason}"[:500],
    )


def _analyst_dicts(analysts: list[AnalystReport]) -> list[dict[str, Any]]:
    return [report.model_dump() for report in analysts]


def run_bull_bear_debate(ctx: RunContext, analysts: list[AnalystReport]) -> DebateResult:
    settings = DebateSettings.from_config(ctx.config)
    budget = settings.round_budget()
    reports = _analyst_dicts(analysts)
    previous: dict[str, list[str]] = {}
    history: list[dict[str, Any]] = []
    arguments: dict[str, ResearchArgument] = {}
    ratio = 1.0
    reason = "max_rounds"
    rounds = 0

    for index in range(budget):
        rounds = index + 1
        current_points: dict[str, list[str]] = {}
        for side in ("bull", "bear"):
            opponent = "bear" if side == "bull" else "bull"
            context = {
                "code": ctx.code,
                "date": ctx.date,
                "role": side,
                "side": side,
                "analysts": reports,
                "digest": ctx.digest,
                "weights": ctx.weights,
                "round": rounds,
                "previous_points": previous.get(side, []),
                "opponent_points": previous.get(opponent, []),
            }
            model = run_role(ctx, side, context)
            if isinstance(model, ResearchArgument):
                argument = model.model_copy(update={"side": side})
            else:
                argument = _fallback_argument(side, "Schema 不是 ResearchArgument")
            arguments[side] = argument
            current_points[side] = list(argument.points) + list(argument.rebuttals)

        if settings.enabled and rounds > 1:
            done, ratios = converged(previous, current_points, min_new_ratio=settings.min_new_ratio)
            ratio = min(ratios.values()) if ratios else 1.0
        else:
            done, ratios = False, {}

        history.append(
            {
                "round": rounds,
                "points": {side: current_points.get(side, []) for side in ("bull", "bear")},
                "new_point_ratio": {key: round(value, 4) for key, value in ratios.items()},
            }
        )
        previous = current_points
        if done:
            reason = "converged"
            break
        if settings.enabled and not settings.max_rounds > rounds:
            reason = "max_rounds"
            break

    if not settings.enabled:
        reason = "convergence_disabled"

    return DebateResult(
        arguments=[arguments[side] for side in ("bull", "bear") if side in arguments],
        rounds_used=rounds,
        convergence_reason=reason,
        new_point_ratio=ratio,
        history=history,
    )


def run_risk_debate(
    ctx: RunContext,
    verdict: ResearchVerdict,
    trade: TradePlan,
    prior_risk: list[RiskArgument] | None = None,
) -> RiskDebateResult:
    settings = DebateSettings.from_config(ctx.config)
    budget = settings.round_budget(risk=True)
    verdict_payload = verdict.model_dump()
    trade_payload = trade.model_dump()
    previous: dict[str, list[str]] = {}
    if prior_risk:
        for argument in prior_risk:
            previous.setdefault(argument.side, []).extend(argument.points)
    history: list[dict[str, Any]] = []
    arguments: dict[str, RiskArgument] = {}
    ratio = 1.0
    reason = "max_rounds"
    rounds = 0

    for index in range(budget):
        rounds = index + 1
        current_points: dict[str, list[str]] = {}
        for side in RISK_SIDES:
            context = {
                "code": ctx.code,
                "date": ctx.date,
                "role": f"risk_{side}",
                "side": side,
                "verdict": verdict_payload,
                "trade": trade_payload,
                "risks": [
                    {"side": other, "points": previous.get(other, [])} for other in RISK_SIDES
                ],
                "digest": ctx.digest,
                "round": rounds,
                "previous_points": previous.get(side, []),
                "other_sides": [sorted(previous.get(other, ())) for other in RISK_SIDES],
            }
            model = run_role(ctx, f"risk_{side}", context)
            if isinstance(model, RiskArgument):
                argument = model.model_copy(update={"side": side})
            else:
                argument = _fallback_risk(side, "Schema 不是 RiskArgument")
            arguments[side] = argument
            current_points[side] = list(argument.points)

        if settings.enabled and rounds > 1:
            done, ratios = converged(previous, current_points, min_new_ratio=settings.min_new_ratio)
            ratio = min(ratios.values()) if ratios else 1.0
        else:
            done, ratios = False, {}

        history.append(
            {
                "round": rounds,
                "points": {side: current_points.get(side, []) for side in RISK_SIDES},
                "new_point_ratio": {key: round(value, 4) for key, value in ratios.items()},
            }
        )
        previous = current_points
        if done:
            reason = "converged"
            break
        if settings.enabled and not settings.max_risk_rounds > rounds:
            reason = "max_rounds"
            break

    if not settings.enabled:
        reason = "convergence_disabled"

    return RiskDebateResult(
        arguments=[arguments[side] for side in RISK_SIDES if side in arguments],
        rounds_used=rounds,
        convergence_reason=reason,
        new_point_ratio=ratio,
        history=history,
    )


__all__ = ["DebateResult", "RISK_SIDES", "RiskDebateResult", "run_bull_bear_debate", "run_risk_debate"]
