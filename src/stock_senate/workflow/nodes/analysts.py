"""Analyst stage: every enabled seat, one report each.

Seats are resolved through the registry (which enforces the 12-seat cap) and
their data domains through the ``ToolGate`` (which enforces the per-role tool
whitelist). A custom role whose prompt file is missing is skipped and marked
``partial`` instead of blocking the whole run.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from stock_senate.roles.schemas import AnalystReport
from stock_senate.workflow.context import RunContext
from stock_senate.workflow.nodes.base import NodeConfigError, run_role
from stock_senate.workflow.verify import apply_conflicts, cross_check


def analyst_context(ctx: RunContext, spec: Any, domains: list[str]) -> dict[str, Any]:
    return {
        "code": ctx.code,
        "date": ctx.date,
        "role": spec.key,
        "display_name": spec.display_name,
        "role_domains": domains,
        "digest": ctx.digest,
        "weights": ctx.weights,
        "as_of": ctx.date,
    }


def run_analysts(ctx: RunContext) -> list[AnalystReport]:
    reports: list[AnalystReport] = []
    for spec in ctx.registry.enabled_analysts():
        domains = ctx.role_domains(spec.key)
        context = analyst_context(ctx, spec, domains)
        try:
            report = run_role(ctx, spec.key, context)
        except NodeConfigError as exc:
            ctx.skipped.append({"role": spec.key, "reason": str(exc)[:300]})
            report = _skipped_report(spec, str(exc))
        if not isinstance(report, AnalystReport):
            report = _skipped_report(spec, "角色输出 Schema 不是 AnalystReport")
        conflicts = cross_check(report.model_dump(), ctx.digest)
        if conflicts:
            report = apply_conflicts(report, conflicts, ctx)
        reports.append(report)
    return reports


def _skipped_report(spec: Any, reason: str) -> AnalystReport:
    return AnalystReport(
        role=spec.key,
        partial=True,
        notes=f"角色已跳过：{reason}"[:500],
        display_name=spec.display_name,
        stance="unknown",
        confidence=0.0,
        summary=f"{spec.display_name}本次未能产出可用结论。",
        data_gaps=[reason[:300]],
    )


def reports_as_dicts(reports: list[AnalystReport]) -> list[dict[str, Any]]:
    return [report.model_dump() for report in reports]


def aggregate_stance(
    reports: list[Mapping[str, Any]], weights: Mapping[str, float]
) -> dict[str, Any]:
    """Weighted stance summary used by the research manager's prompt/context."""
    weight_total = 0.0
    score = 0.0
    for report in reports:
        weight = float(weights.get(str(report.get("role")), 1.0))
        value = {"bullish": 1.0, "bearish": -1.0}.get(str(report.get("stance")), 0.0)
        confidence = float(report.get("confidence") or 0.0)
        weight_total += weight * max(0.2, confidence)
        score += weight * value * max(0.2, confidence)
    normalized = score / weight_total if weight_total else 0.0
    return {
        "weighted_score": round(normalized, 4),
        "bullish": sum(1 for item in reports if item.get("stance") == "bullish"),
        "bearish": sum(1 for item in reports if item.get("stance") == "bearish"),
        "neutral": sum(1 for item in reports if item.get("stance") == "neutral"),
        "unknown": sum(1 for item in reports if item.get("stance") == "unknown"),
    }


__all__ = ["aggregate_stance", "analyst_context", "reports_as_dicts", "run_analysts"]
