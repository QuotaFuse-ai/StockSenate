"""``report diff``: field-level comparison of two saved snapshots.

The comparison is deliberately structured-only (rating, horizon, confidence,
quality, data status) plus set deltas for risks, reasons and analyst stances,
so a reworded sentence never looks like a changed conclusion. These tests seed
real snapshots through ``save_run`` — the same path the CLI reads — and then
check both the store API and the ``report diff`` subprocess.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cli_support import CODE, cli_env, parse_json, run_cli

from stock_senate.report import store
from stock_senate.report.diff import diff_states
from stock_senate.report.store import ReportNotFound, save_run

BEFORE_DAY = "2026-01-27"
AFTER_DAY = "2026-01-28"


def _state(
    run_id: str,
    *,
    date: str,
    rating: str,
    horizon: str = "中期",
    confidence: float = 0.5,
    low_quality: bool = False,
    score: float = 70.0,
    stance: str = "neutral",
    direction: str = "观望",
    status: str = "ok",
    risks: tuple[str, ...] = (),
    reasons: tuple[str, ...] = (),
    analysts: tuple[tuple[str, str], ...] = (),
    generated_at: str | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "code": CODE,
        "date": date,
        "generated_at": generated_at or f"{date}T09:00:00",
        "partial": low_quality,
        "decision": {
            "run_id": run_id,
            "code": CODE,
            "date": date,
            "rating": rating,
            "horizon": horizon,
            "confidence": confidence,
            "low_quality": low_quality,
            "risks": list(risks),
            "reasons": list(reasons),
        },
        "quality": {"score": score, "threshold": 70.0, "partial": low_quality},
        "research": {"stance": stance},
        "trade": {"direction": direction},
        "data": {"summary": {"status": status}},
        "analysts": [{"role": role, "stance": value} for role, value in analysts],
        "skipped": [],
        "budget": {},
        "hitl": {},
    }


def _field(payload: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in payload["fields"] if item["field"] == name)


# --------------------------------------------------------------------------- #
# diff_states
# --------------------------------------------------------------------------- #


def test_every_structured_field_is_compared_in_fixed_order() -> None:
    before = _state("run-hold", date=BEFORE_DAY, rating="hold")
    after = _state(
        "run-buy",
        date=AFTER_DAY,
        rating="buy",
        confidence=0.8,
        score=85.0,
        stance="bullish",
    )

    payload = diff_states(before, after)

    assert [item["field"] for item in payload["fields"]] == [
        "decision.rating",
        "decision.horizon",
        "decision.confidence",
        "decision.low_quality",
        "quality.score",
        "research.stance",
        "trade.direction",
        "data.status",
    ]
    assert _field(payload, "decision.rating") == {
        "field": "decision.rating",
        "from": "hold",
        "to": "buy",
        "changed": True,
    }
    assert _field(payload, "decision.horizon")["changed"] is False
    assert _field(payload, "decision.confidence")["changed"] is True
    assert _field(payload, "quality.score")["changed"] is True
    assert _field(payload, "trade.direction")["changed"] is False
    assert payload["changed_fields"] == [
        "decision.rating",
        "decision.confidence",
        "quality.score",
        "research.stance",
    ]
    assert payload["changed_count"] == 4


def test_a_downgrade_reports_the_direction_as_down() -> None:
    payload = diff_states(
        _state("run-buy", date=BEFORE_DAY, rating="buy"),
        _state("run-under", date=AFTER_DAY, rating="underweight"),
    )

    assert payload["conclusion"] == {"changed": True, "direction": "down"}
    assert _field(payload, "decision.rating")["from"] == "buy"
    assert _field(payload, "decision.rating")["to"] == "underweight"


def test_risks_and_reasons_are_compared_as_sets() -> None:
    before = _state(
        "run-hold",
        date=BEFORE_DAY,
        rating="hold",
        risks=("估值偏高", "解禁压力"),
        reasons=("盈利稳定",),
    )
    after = _state(
        "run-hold2",
        date=AFTER_DAY,
        rating="hold",
        risks=("解禁压力", "原材料涨价"),
        reasons=("盈利稳定", "渠道回暖"),
    )

    payload = diff_states(before, after)

    assert payload["risks"] == {
        "field": "decision.risks",
        "added": ["原材料涨价"],
        "removed": ["估值偏高"],
        "changed": True,
    }
    assert payload["reasons"]["added"] == ["渠道回暖"]
    assert payload["reasons"]["removed"] == []
    assert payload["changed_fields"] == ["decision.risks", "decision.reasons"]
    assert payload["conclusion"] == {"changed": False, "direction": "unchanged"}


def test_analyst_stance_changes_are_listed_per_role() -> None:
    before = _state(
        "run-hold",
        date=BEFORE_DAY,
        rating="hold",
        analysts=(("market", "neutral"), ("news", "bullish")),
    )
    after = _state(
        "run-buy",
        date=AFTER_DAY,
        rating="buy",
        analysts=(("market", "bearish"), ("news", "bullish"), ("fundamentals", "bullish")),
    )

    payload = diff_states(before, after)

    assert payload["analysts"] == [
        {"role": "fundamentals", "from": None, "to": "bullish"},
        {"role": "market", "from": "neutral", "to": "bearish"},
    ]
    assert "analysts" in payload["changed_fields"]


def test_identical_runs_report_no_change() -> None:
    state = _state("run-hold", date=BEFORE_DAY, rating="hold", risks=("解禁压力",))

    payload = diff_states(state, {**state, "run_id": "run-hold-repeat"})

    assert payload["changed_fields"] == []
    assert payload["changed_count"] == 0
    assert payload["conclusion"] == {"changed": False, "direction": "unchanged"}
    assert payload["risks"]["changed"] is False


# --------------------------------------------------------------------------- #
# store.diff: newest snapshot per date
# --------------------------------------------------------------------------- #


def test_store_uses_the_newest_snapshot_of_each_date(tmp_path: Path) -> None:
    home = tmp_path / "home"
    save_run(
        _state(
            "run-hold-old",
            date=BEFORE_DAY,
            rating="lean_hold",
            generated_at=f"{BEFORE_DAY}T08:00:00",
        ),
        home=home,
    )
    save_run(
        _state(
            "run-hold-new",
            date=BEFORE_DAY,
            rating="hold",
            generated_at=f"{BEFORE_DAY}T10:00:00",
        ),
        home=home,
    )
    save_run(_state("run-buy", date=AFTER_DAY, rating="buy"), home=home)

    payload = store.diff(CODE, BEFORE_DAY, AFTER_DAY, home=home)

    assert payload["mode"] == "diff"
    assert payload["code"] == CODE
    assert payload["from"] == {"run_id": "run-hold-new", "date": BEFORE_DAY, "rating": "hold"}
    assert payload["to"] == {"run_id": "run-buy", "date": AFTER_DAY, "rating": "buy"}
    assert payload["conclusion"] == {"changed": True, "direction": "up"}


def test_a_date_without_a_snapshot_is_reported(tmp_path: Path) -> None:
    home = tmp_path / "home"
    save_run(_state("run-buy", date=AFTER_DAY, rating="buy"), home=home)

    with pytest.raises(ReportNotFound) as excinfo:
        store.diff(CODE, BEFORE_DAY, AFTER_DAY, home=home)

    assert BEFORE_DAY in str(excinfo.value)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_report_diff_emits_the_comparison(tmp_path: Path) -> None:
    home = tmp_path / "home"
    save_run(_state("run-hold", date=BEFORE_DAY, rating="hold"), home=home)
    save_run(
        _state(
            "run-buy",
            date=AFTER_DAY,
            rating="buy",
            confidence=0.8,
            score=85.0,
            risks=("解禁压力",),
        ),
        home=home,
    )

    result = run_cli(
        "report",
        "diff",
        "--code",
        CODE,
        "--from",
        BEFORE_DAY,
        "--to",
        AFTER_DAY,
        "--output",
        "json",
        env=cli_env(home=home),
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    payload = parse_json(result)
    assert payload["code"] == CODE
    assert payload["from"]["run_id"] == "run-hold"
    assert payload["to"]["run_id"] == "run-buy"
    assert payload["conclusion"] == {"changed": True, "direction": "up"}
    assert "decision.rating" in payload["changed_fields"]


def test_cli_report_diff_requires_all_three_arguments(tmp_path: Path) -> None:
    result = run_cli(
        "report",
        "diff",
        "--code",
        CODE,
        "--from",
        BEFORE_DAY,
        env=cli_env(home=tmp_path / "home"),
    )

    assert result.returncode == 2
    assert "--to" in result.stderr or "需要" in result.stderr
    assert result.stdout == ""
