"""Quality gate scoring: deterministic rules plus an optional rubric judge.

Three modes, chosen by ``workflow.quality_gate.mode``:

``rules``
    Score the chain from its own evidence - domain coverage, citation validity
    and chain completeness. No model call, so the result is reproducible and
    costs nothing.
``judge``
    Ask the ``quality_gate`` role for an independent rubric score. The rubric
    lives in ``prompts/managers/quality_gate.md``; the judge sees the finished
    chain and returns per-dimension scores, defects and compliance flags.
``hybrid``
    Run both and blend them with ``judge_weight``. Either side can mark the run
    ``low_quality``: a rules blocker is never outvoted by a generous judge.

Both model-driven modes degrade instead of failing: an unreachable model, an
offline provider or an unusable object falls back to the rules verdict with a
note saying so, because a finished analysis must not be lost at its last step.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from stock_senate.config.schema import QUALITY_GATE_MODES
from stock_senate.roles.schemas import QualityDimension, QualityVerdict
from stock_senate.workflow import offline

RUBRIC_DIMENSIONS = ("logic_coherence", "evidence_sufficiency", "citation_accuracy")
MODES = QUALITY_GATE_MODES
DEFAULT_MODE = "rules"
DEFAULT_JUDGE_WEIGHT = 0.5


def gate_settings(config: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(((config or {}).get("workflow") or {}).get("quality_gate") or {})


def normalize_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in MODES else DEFAULT_MODE


def normalize_weight(value: Any) -> float:
    try:
        weight = float(value)
    except (TypeError, ValueError):
        return DEFAULT_JUDGE_WEIGHT
    return min(1.0, max(0.0, weight))


def rule_verdict(context: Mapping[str, Any]) -> QualityVerdict:
    """The deterministic score, computed from the context the judge would see."""
    return QualityVerdict.model_validate(offline.quality_verdict(dict(context)))


def judge_provider(resolver: Any) -> str:
    try:
        target = resolver.target("quality_gate")
    except (AttributeError, KeyError, ValueError):
        return ""
    return str(getattr(target, "provider", "") or "").strip().lower()


def judge_usable(verdict: QualityVerdict) -> bool:
    """A judge that could not score has no dimensions and is marked partial."""
    return not verdict.partial and bool(verdict.dimensions)


def finalize_judge(
    judge: QualityVerdict, rules: QualityVerdict, *, threshold: float
) -> QualityVerdict:
    """Judge score, rules sanity: structural data gaps and compliance stick."""
    notes = [judge.notes] if judge.notes else []
    low_quality = judge.low_quality or rules.low_quality or judge.score < threshold
    compliance = dict(judge.compliance)
    if compliance.get("price_levels_found"):
        low_quality = True
        notes.append("评审发现可执行价位或交易指令表述，已标记 low_quality。")
    if rules.partial:
        notes.append("存在无事实的数据域，规则检查标记 partial 并要求人工复核。")
    return judge.model_copy(
        update={
            "mode": "judge",
            "threshold": threshold,
            "low_quality": low_quality,
            "partial": judge.partial or rules.partial,
            "notes": " ".join(notes).strip(),
        }
    )


def combine(
    rules: QualityVerdict,
    judge: QualityVerdict,
    *,
    weight: float = DEFAULT_JUDGE_WEIGHT,
    threshold: float = 70.0,
) -> QualityVerdict:
    """Blend the two scores dimension by dimension."""
    share = normalize_weight(weight)
    score = round(rules.score * (1 - share) + judge.score * share, 1)
    dimensions: dict[str, QualityDimension] = {}
    for name in sorted(set(rules.dimensions) | set(judge.dimensions)):
        base = rules.dimensions.get(name)
        opinion = judge.dimensions.get(name)
        if base is None:
            dimensions[name] = opinion  # type: ignore[assignment]
        elif opinion is None:
            dimensions[name] = base
        else:
            dimensions[name] = QualityDimension(
                score=round(base.score * (1 - share) + opinion.score * share, 1),
                note="；".join(part for part in (base.note, opinion.note) if part)[:300],
            )

    notes = [
        f"规则分 {rules.score:.1f} 与评审分 {judge.score:.1f} "
        f"按 {share:.0%} 权重合并为 {score:.1f}。"
    ]
    if judge.notes:
        notes.append(judge.notes)
    compliance = dict(judge.compliance)
    low_quality = rules.low_quality or judge.low_quality or score < threshold
    if compliance.get("price_levels_found"):
        low_quality = True
        notes.append("评审发现可执行价位或交易指令表述，已标记 low_quality。")
    if rules.partial:
        notes.append("存在无事实的数据域，规则检查标记 partial 并要求人工复核。")
    return QualityVerdict(
        role="quality_gate",
        partial=rules.partial or judge.partial,
        score=score,
        low_quality=low_quality,
        threshold=threshold,
        dimensions=dimensions,
        notes=" ".join(notes).strip()[:500],
        mode="hybrid",
        defects=list(judge.defects),
        compliance=compliance,
    )


__all__ = [
    "DEFAULT_JUDGE_WEIGHT",
    "DEFAULT_MODE",
    "MODES",
    "RUBRIC_DIMENSIONS",
    "combine",
    "finalize_judge",
    "gate_settings",
    "judge_provider",
    "judge_usable",
    "normalize_mode",
    "normalize_weight",
    "rule_verdict",
]
