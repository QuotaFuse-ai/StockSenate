"""Field-level comparison of two saved runs.

``ssenate report diff`` answers three questions, in that order:

1. what changed field by field (rating, horizon, confidence, quality, data),
2. whether the conclusion changed,
3. which risks and reasons appeared or disappeared.

Only structured fields are compared; free text is never diffed line by line, so
a reworded sentence does not masquerade as a changed conclusion.
"""

from __future__ import annotations

from typing import Any, Mapping

_RATING_ORDER = ("sell", "underweight", "hold", "overweight", "buy")


def _get(state: Mapping[str, Any] | None, *path: str) -> Any:
    node: Any = state
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def _normal(value: Any) -> Any:
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, float):
        return round(value, 4)
    return value


def _field(label: str, a: Any, b: Any) -> dict[str, Any]:
    left, right = _normal(a), _normal(b)
    return {"field": label, "from": left, "to": right, "changed": left != right}


def _set_delta(label: str, a: list[Any], b: list[Any]) -> dict[str, Any]:
    left = {str(item) for item in (a or []) if str(item).strip()}
    right = {str(item) for item in (b or []) if str(item).strip()}
    return {
        "field": label,
        "added": sorted(right - left),
        "removed": sorted(left - right),
        "changed": left != right,
    }


def _analyst_stances(state: Mapping[str, Any] | None) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for report in (state or {}).get("analysts") or []:
        key = str(report.get("role") or report.get("display_name") or "")
        if key:
            rows[key] = _normal(report.get("stance"))
    return rows


def _direction_shift(a: str | None, b: str | None) -> str:
    def rank(value: Any) -> int | None:
        try:
            return _RATING_ORDER.index(str(value))
        except ValueError:
            return None

    left, right = rank(a), rank(b)
    if left is None or right is None:
        return "unknown"
    if right > left:
        return "up"
    if right < left:
        return "down"
    return "unchanged"


def diff_states(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    limit: int = 50,
) -> dict[str, Any]:
    """Compare two ``state.json`` bodies."""
    fields = [
        _field("decision.rating", _get(before, "decision", "rating"), _get(after, "decision", "rating")),
        _field("decision.horizon", _get(before, "decision", "horizon"), _get(after, "decision", "horizon")),
        _field("decision.confidence", _get(before, "decision", "confidence"), _get(after, "decision", "confidence")),
        _field("decision.low_quality", _get(before, "decision", "low_quality"), _get(after, "decision", "low_quality")),
        _field("quality.score", _get(before, "quality", "score"), _get(after, "quality", "score")),
        _field("research.stance", _get(before, "research", "stance"), _get(after, "research", "stance")),
        _field("trade.direction", _get(before, "trade", "direction"), _get(after, "trade", "direction")),
        _field("data.status", _get(before, "data", "summary", "status"), _get(after, "data", "summary", "status")),
    ]

    stances_before = _analyst_stances(before)
    stances_after = _analyst_stances(after)
    analyst_changes = []
    for role in sorted(set(stances_before) | set(stances_after)):
        if stances_before.get(role) != stances_after.get(role):
            analyst_changes.append(
                {"role": role, "from": stances_before.get(role), "to": stances_after.get(role)}
            )

    risks = _set_delta("decision.risks", _get(before, "decision", "risks") or [], _get(after, "decision", "risks") or [])
    reasons = _set_delta("decision.reasons", _get(before, "decision", "reasons") or [], _get(after, "decision", "reasons") or [])

    changed = [item["field"] for item in fields if item["changed"]]
    if risks["changed"]:
        changed.append(risks["field"])
    if reasons["changed"]:
        changed.append(reasons["field"])
    if analyst_changes:
        changed.append("analysts")

    return {
        "mode": "diff",
        "code": after.get("code") or before.get("code"),
        "from": {
            "run_id": before.get("run_id"),
            "date": before.get("date"),
            "rating": _get(before, "decision", "rating"),
        },
        "to": {
            "run_id": after.get("run_id"),
            "date": after.get("date"),
            "rating": _get(after, "decision", "rating"),
        },
        "fields": fields,
        "analysts": analyst_changes[:limit],
        "risks": risks,
        "reasons": reasons,
        "changed_fields": changed,
        "changed_count": len(changed),
        "conclusion": {
            "changed": _get(before, "decision", "rating") != _get(after, "decision", "rating"),
            "direction": _direction_shift(
                _get(before, "decision", "rating"), _get(after, "decision", "rating")
            ),
        },
    }


__all__ = ["diff_states"]
