"""Post-hoc attribution of saved decisions — explicitly not a backtest.

``ssenate performance`` answers "what did the senate actually decide, and how
consistent was it": rating mix, confidence distribution, quality and data
coverage, analyst stances, and how often a run needed human review.

It never reports an equity curve, net value or return. Those belong to
``ssenate backtest run`` (DEC-004); mixing the two would let an attribution
table look like a performance claim.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from statistics import fmean
from typing import Any

from stock_senate.report.store import ReportNotFound, list_runs, load

DISCLAIMER = (
    "事后归因：统计已保存决策的过程指标，不含净值、收益曲线或任何回测收益。"
    "回测请使用 `ssenate backtest run`。"
)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return round(ordered[index], 4)


def _mean(values: list[float]) -> float | None:
    return round(fmean(values), 4) if values else None


def _row(state: dict[str, Any]) -> dict[str, Any]:
    decision = dict(state.get("decision") or {})
    quality = dict(state.get("quality") or {})
    summary = dict((state.get("data") or {}).get("summary") or {})
    return {
        "run_id": state.get("run_id"),
        "code": state.get("code"),
        "date": state.get("date"),
        "rating": decision.get("rating"),
        "horizon": decision.get("horizon"),
        "confidence": decision.get("confidence"),
        "quality_score": quality.get("score"),
        "low_quality": bool(decision.get("low_quality")),
        "partial": bool(decision.get("partial") or state.get("partial")),
        "data_status": summary.get("status"),
        "data_missing": len(list(summary.get("missing") or [])),
        "risk_count": len(list(decision.get("risks") or [])),
    }


def summarize(run_id: str | None = None, *, home: Path | str | None = None) -> dict[str, Any]:
    """Aggregate one run or every saved run into an attribution table."""
    if run_id:
        states = [load(run_id, home=home)]
    else:
        rows = list_runs(home=home)
        states = []
        for row in rows:
            try:
                states.append(load(str(row["run_id"]), home=home))
            except (ReportNotFound, OSError):
                continue

    if run_id and not states:
        raise ReportNotFound(f"找不到报告快照 {run_id}")

    records = [_row(state) for state in states]
    ratings = Counter(str(item["rating"] or "unknown") for item in records)
    actions = Counter(str((state.get("decision") or {}).get("action") or "未知") for state in states)
    statuses = Counter(str(item["data_status"] or "missing") for item in records)
    confidences = [float(item["confidence"]) for item in records if item["confidence"] is not None]
    quality_scores = [float(item["quality_score"]) for item in records if item["quality_score"] is not None]

    stance_counts: dict[str, Counter[str]] = {}
    for state in states:
        for report in state.get("analysts") or []:
            role = str(report.get("role") or "")
            if role:
                stance_counts.setdefault(role, Counter())[str(report.get("stance") or "unknown")] += 1

    attribution_by_rating: dict[str, dict[str, Any]] = {}
    for rating in sorted(ratings):
        subset = [item for item in records if str(item["rating"] or "unknown") == rating]
        attribution_by_rating[rating] = {
            "runs": len(subset),
            "mean_confidence": _mean([float(item["confidence"]) for item in subset if item["confidence"] is not None]),
            "mean_quality": _mean([float(item["quality_score"]) for item in subset if item["quality_score"] is not None]),
            "low_quality_runs": sum(1 for item in subset if item["low_quality"]),
        }

    return {
        "mode": "performance",
        "scope": run_id or "all",
        "disclaimer": DISCLAIMER,
        "runs": len(records),
        "codes": len({item["code"] for item in records if item["code"]}),
        "ratings": dict(sorted(ratings.items())),
        "actions": dict(sorted(actions.items())),
        "confidence": {
            "mean": _mean(confidences),
            "min": round(min(confidences), 4) if confidences else None,
            "max": round(max(confidences), 4) if confidences else None,
            "p50": _percentile(confidences, 0.5),
        },
        "quality": {
            "mean": _mean(quality_scores),
            "low_quality_runs": sum(1 for item in records if item["low_quality"]),
        },
        "partial_runs": sum(1 for item in records if item["partial"]),
        "data_status": dict(sorted(statuses.items())),
        "analyst_stances": {
            role: dict(sorted(counts.items())) for role, counts in sorted(stance_counts.items())
        },
        "attribution": attribution_by_rating,
        "history": records,
    }


__all__ = ["DISCLAIMER", "summarize"]
