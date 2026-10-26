"""Debate convergence helpers.

A debate stops either because a fixed round limit is reached or because both
sides have stopped producing new arguments. The second test is deliberately
conservative: a round only counts as converged when *every* side's share of new
points drops below ``convergence_min_new_points_ratio``. Otherwise a quiet bull
and a still-escalating bear would end the debate early.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

_PUNCTUATION = re.compile(r"[\s，。；：、！？,.;:!?\"'“”‘’（）()\[\]【】<>《》\-—_]+")


def normalize_point(text: Any) -> str:
    return _PUNCTUATION.sub("", str(text or "")).lower()


def point_set(points: Iterable[Any]) -> set[str]:
    return {normalized for item in points if (normalized := normalize_point(item))}


def new_point_ratio(current: Iterable[Any], previous: Iterable[Any]) -> float:
    current_set = point_set(current)
    if not current_set:
        return 0.0
    return len(current_set - point_set(previous)) / len(current_set)


@dataclass(frozen=True)
class DebateSettings:
    max_rounds: int
    max_risk_rounds: int
    enabled: bool
    min_new_ratio: float
    convergence_max_rounds: int

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> DebateSettings:
        section = ((config or {}).get("workflow") or {}).get("debate") or {}
        return cls(
            max_rounds=max(1, int(section.get("max_debate_rounds", 2))),
            max_risk_rounds=max(1, int(section.get("max_risk_discuss_rounds", 2))),
            enabled=section.get("convergence_enabled", True) is not False,
            min_new_ratio=float(section.get("convergence_min_new_points_ratio", 0.2)),
            convergence_max_rounds=max(1, int(section.get("convergence_max_rounds", 5))),
        )

    def round_budget(self, *, risk: bool = False) -> int:
        limit = self.max_risk_rounds if risk else self.max_rounds
        if self.enabled:
            limit = min(limit, self.convergence_max_rounds)
        return max(1, limit)


def converged(
    previous: Mapping[str, Iterable[Any]],
    current: Mapping[str, Iterable[Any]],
    *,
    min_new_ratio: float,
) -> tuple[bool, dict[str, float]]:
    """True when no side contributed enough new points to justify another round."""
    if not previous:
        return False, {}
    ratios = {
        side: new_point_ratio(current.get(side, ()), previous.get(side, ()))
        for side in current
    }
    if not ratios:
        return False, {}
    return all(ratio < min_new_ratio for ratio in ratios.values()), ratios


__all__ = [
    "DebateSettings",
    "converged",
    "new_point_ratio",
    "normalize_point",
    "point_set",
]
