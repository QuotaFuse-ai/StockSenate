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

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Stance = Literal["bullish", "neutral", "bearish", "unknown"]
Rating = Literal["buy", "overweight", "hold", "underweight", "sell", "unknown"]
Horizon = Literal["intraday", "short", "medium", "long", "unknown"]


#: 枚举字段：模型常按提示词写成 `Hold` / `Bull` 这种首字母大写形式，
#: 而契约（以及下游落库）只认小写字面量。统一在入口做大小写归一，
#: 避免把「大小写」当成「格式错误」把整个角色判成 partial。
ENUM_FIELDS = frozenset({"side", "stance", "rating", "horizon", "direction", "severity"})

#: 提示词里这些字段要求「每条一句」，模型常给成
#: ``{"category": "growth", "point": "..."}`` 这种对象。契约是 ``list[str]``，
#: 因此在这一层把对象压平成一句可读文本，而不是把整个角色判成不可用。
TEXT_LIST_FIELDS = frozenset(
    {
        "key_points",
        "points",
        "rebuttals",
        "rationale",
        "risks",
        "reasons",
        "unresolved",
        "data_gaps",
        "risk_flags",
    }
)

#: 提示词把 confidence 描述为 high / medium / low，契约是 0–1 浮点，这里做映射。
CONFIDENCE_LEVELS = {
    "high": 0.8,
    "medium": 0.5,
    "low": 0.2,
    "unknown": 0.0,
    "较高": 0.75,
    "中等": 0.5,
    "较低": 0.25,
    "高": 0.8,
    "中": 0.5,
    "低": 0.2,
}


def _flatten_text_item(item: Any) -> str:
    """把模型给出的单条要点压成一行文本，保留可读顺序。"""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        parts = [str(v).strip() for v in item.values() if isinstance(v, (str, int, float))]
        parts = [p for p in parts if p]
        return "：".join(parts) if parts else str(item)
    return str(item)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _coerce_loose_fields(cls, data: Any) -> Any:
        """把「格式不一致」挡在契约之外，而不是把整个角色判成 partial。

        覆盖三类提示词与契约之间的表述差异：枚举大小写、confidence 档位词、
        文本列表被写成对象。
        """
        if not isinstance(data, dict):
            return data
        out = dict(data)

        for key in ENUM_FIELDS:
            value = out.get(key)
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized != value:
                    out[key] = normalized

        confidence = out.get("confidence")
        if isinstance(confidence, str):
            token = confidence.strip().lower()
            if token in CONFIDENCE_LEVELS:
                out["confidence"] = CONFIDENCE_LEVELS[token]
            elif token.endswith("%"):
                try:
                    out["confidence"] = max(0.0, min(1.0, float(token[:-1]) / 100.0))
                except ValueError:
                    pass

        for key in TEXT_LIST_FIELDS:
            value = out.get(key)
            if isinstance(value, str):
                out[key] = [value]
            elif isinstance(value, list) and any(not isinstance(x, str) for x in value):
                out[key] = [_flatten_text_item(x) for x in value]

        return out


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
