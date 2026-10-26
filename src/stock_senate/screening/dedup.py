"""Merge screening candidates from several strategies into unique codes.

Every strategy may emit the same instrument under a different spelling
(``sh600519`` / ``600519.SH``). The merge normalises each code once and keeps
the first-seen order, so downstream batching never analyses the same code
twice. A duplicated code keeps the union of its strategies and the best score.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from stock_senate.data.codes import normalize_code


@dataclass
class Candidate:
    code: str
    strategies: tuple[str, ...] = ()
    score: float | None = None
    name: str = ""

    def as_row(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "strategies": list(self.strategies),
            "score": self.score,
            "name": self.name,
        }


def dedup_codes(codes: Iterable[str]) -> list[str]:
    """Return normalised codes with duplicates removed, order preserved."""
    seen: dict[str, None] = {}
    for code in codes:
        seen.setdefault(normalize_code(code), None)
    return list(seen)


def merge_candidates(*groups: Iterable[Candidate | Mapping[str, Any]]) -> list[Candidate]:
    """Merge strategy outputs into one candidate per code."""
    merged: dict[str, Candidate] = {}
    for group in groups:
        for raw in group:
            candidate = _coerce(raw)
            existing = merged.get(candidate.code)
            if existing is None:
                merged[candidate.code] = candidate
                continue
            strategies = list(existing.strategies)
            for strategy in candidate.strategies:
                if strategy and strategy not in strategies:
                    strategies.append(strategy)
            existing.strategies = tuple(strategies)
            existing.score = _best(existing.score, candidate.score)
            existing.name = existing.name or candidate.name
    return list(merged.values())


def _coerce(raw: Candidate | Mapping[str, Any]) -> Candidate:
    if isinstance(raw, Candidate):
        return Candidate(
            code=normalize_code(raw.code),
            strategies=tuple(str(item) for item in raw.strategies if str(item)),
            score=_score(raw.score),
            name=str(raw.name or ""),
        )
    if not isinstance(raw, Mapping):
        raise ValueError(f"候选必须是 Candidate 或映射：{raw!r}")
    strategies = raw.get("strategies")
    if strategies is None:
        strategies = [raw.get("strategy") or ""]
    elif isinstance(strategies, str):
        strategies = [strategies]
    return Candidate(
        code=normalize_code(str(raw.get("code") or "")),
        strategies=tuple(str(item) for item in strategies if str(item)),
        score=_score(raw.get("score")),
        name=str(raw.get("name") or ""),
    )


def _score(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"候选分数不是数值：{value!r}") from exc


def _best(left: float | None, right: float | None) -> float | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


__all__ = ["Candidate", "dedup_codes", "merge_candidates"]
