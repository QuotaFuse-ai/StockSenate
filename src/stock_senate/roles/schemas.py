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

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class PlainClaim(StrictModel):
    """One plain-language statement plus the citations that back it.

    ``neutral=True`` marks a sentence that makes no analytical claim (a
    "no verifiable source" notice, for instance); every other statement must
    carry at least one citation, which is enforced here rather than left to
    the builder.
    """

    text: str
    evidence: list[Citation] = Field(default_factory=list)
    neutral: bool = False

    @model_validator(mode="after")
    def _evidence_or_neutral(self) -> PlainClaim:
        if not self.neutral and not self.evidence:
            raise ValueError("白话结论中的论断必须引用至少一条证据")
        return self


class PlainGlossaryEntry(StrictModel):
    """A jargon term and its one-sentence plain-language note."""

    term: str
    note: str


class PlainConclusion(StrictModel):
    """The plain-language layer: what / why / risks, evidence-first."""

    version: str = "plain-v1"
    what: PlainClaim
    why: list[PlainClaim] = Field(default_factory=list)
    risks: list[PlainClaim] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    confidence_note: str = ""
    glossary: list[PlainGlossaryEntry] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    degraded: bool = False
    omitted_claims: int = 0
    redline_blocked: int = 0


class FinalDecision(RoleOutput):
    rating: Rating = "unknown"
    horizon: Horizon = "unknown"
    reasons: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    low_quality: bool = False
    hitl: dict = Field(default_factory=dict)
    # Filled by the post-processing layer, never by a model: an optional field
    # keeps snapshots written before QUOT-536 loadable.
    plain: PlainConclusion | None = None


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
    "PlainClaim",
    "PlainConclusion",
    "PlainGlossaryEntry",
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
