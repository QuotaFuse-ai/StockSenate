"""Pydantic contracts for every role output.

Each node validates its model output against one of these models. Validation
failure is not fatal: the node retries locally and, if the model still returns
nothing usable, marks the result ``partial`` so the decision can never silently
lose a role.

Boundary rule: a trade plan carries direction, horizon and reasoning only.
Price levels, position sizes and order identifiers are not fields here, and
``extra="forbid"`` turns any attempt to add them into a validation error.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Stance = Literal["bullish", "neutral", "bearish", "unknown"]
Rating = Literal["buy", "overweight", "hold", "underweight", "sell", "unknown"]
Horizon = Literal["intraday", "short", "medium", "long", "unknown"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Citation(StrictModel):
    domain: str
    source: str
    data_as_of: str
    detail: str = ""


class Fact(StrictModel):
    """A value copied from a structured payload, never computed by a model."""

    label: str
    value: str
    domain: str
    data_as_of: str


class RoleOutput(StrictModel):
    role: str
    partial: bool = False
    notes: str = ""


class AnalystReport(RoleOutput):
    display_name: str = ""
    stance: Stance = "unknown"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)


class ResearchArgument(RoleOutput):
    side: Literal["bull", "bear"]
    points: list[str] = Field(default_factory=list)
    rebuttals: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class ResearchVerdict(RoleOutput):
    stance: Stance = "unknown"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    rounds_used: int = 0
    convergence_reason: str = ""


class TradePlan(RoleOutput):
    direction: Literal["long", "short", "flat", "unknown"] = "unknown"
    horizon: Horizon = "unknown"
    rationale: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class RiskArgument(RoleOutput):
    side: Literal["aggressive", "neutral", "conservative"]
    stance: Stance = "unknown"
    points: list[str] = Field(default_factory=list)
    severity: Literal["high", "mid", "low", "unknown"] = "unknown"


class FinalDecision(RoleOutput):
    rating: Rating = "unknown"
    horizon: Horizon = "unknown"
    reasons: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    low_quality: bool = False
    hitl: dict = Field(default_factory=dict)


class QualityDimension(StrictModel):
    score: float = Field(ge=0.0, le=100.0)
    note: str = ""


class QualityDefect(StrictModel):
    """One located flaw from the judge: what is wrong and where."""

    dimension: str = ""
    location: str = ""
    issue: str = ""
    fix_hint: str = ""


class QualityVerdict(RoleOutput):
    score: float = Field(default=0.0, ge=0.0, le=100.0)
    low_quality: bool = False
    threshold: float = 70.0
    dimensions: dict[str, QualityDimension] = Field(default_factory=dict)
    notes: str = ""
    # "rules" | "judge" | "hybrid": which gate produced this verdict.
    mode: str = "rules"
    defects: list[QualityDefect] = Field(default_factory=list)
    compliance: dict[str, bool] = Field(default_factory=dict)


SCHEMAS: dict[str, type[BaseModel]] = {
    "AnalystReport": AnalystReport,
    "ResearchArgument": ResearchArgument,
    "ResearchVerdict": ResearchVerdict,
    "TradePlan": TradePlan,
    "RiskArgument": RiskArgument,
    "FinalDecision": FinalDecision,
    "QualityVerdict": QualityVerdict,
}


def schema_for(name: str) -> type[BaseModel]:
    try:
        return SCHEMAS[name]
    except KeyError as exc:
        raise ValueError(f"未知输出 Schema: {name}") from exc


__all__ = [
    "AnalystReport",
    "Citation",
    "Fact",
    "FinalDecision",
    "Horizon",
    "QualityDefect",
    "QualityDimension",
    "QualityVerdict",
    "Rating",
    "ResearchArgument",
    "ResearchVerdict",
    "RiskArgument",
    "SCHEMAS",
    "Stance",
    "StrictModel",
    "TradePlan",
    "schema_for",
]
