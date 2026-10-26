"""Screening merge: one candidate per instrument after normalisation.

Traceability row 5.4 — several strategies may emit the same code under
different spellings (``sh600519`` / ``600519.SH``), and downstream batching
must never analyse it twice.
"""

from __future__ import annotations

import pytest

from stock_senate.screening.dedup import Candidate, dedup_codes, merge_candidates


def test_dedup_codes_normalises_and_preserves_first_seen_order() -> None:
    assert dedup_codes(["sh600519", "600519.SH", "000001", "sz000001"]) == [
        "600519",
        "000001",
    ]
    assert dedup_codes([]) == []


def test_dedup_codes_rejects_a_non_a_share_code() -> None:
    with pytest.raises(ValueError, match="非法 A 股代码"):
        dedup_codes(["60051"])
    with pytest.raises(ValueError, match="非法 A 股代码"):
        dedup_codes(["AAPL"])


def test_merge_unions_strategies_keeps_best_score_and_first_seen_order() -> None:
    merged = merge_candidates(
        [
            Candidate("sh600519", ("momentum",), 0.6),
            {"code": "000001", "strategy": "value", "score": 0.4},
        ],
        [
            {"code": "600519.SH", "strategies": ["limit_up"], "score": 0.8, "name": "贵州茅台"},
            Candidate("sz000001", ("growth",), None),
        ],
    )

    rows = {item.code: item for item in merged}
    assert list(rows) == ["600519", "000001"]
    assert rows["600519"].strategies == ("momentum", "limit_up")
    assert rows["600519"].score == 0.8
    assert rows["600519"].name == "贵州茅台"
    assert rows["000001"].strategies == ("value", "growth")
    assert rows["000001"].score == 0.4


def test_merge_does_not_mutate_the_input_candidates() -> None:
    original = Candidate("600519", ("momentum",), 0.1)
    merge_candidates([original], [Candidate("600519", ("value",), 0.9)])
    assert original.strategies == ("momentum",)
    assert original.score == 0.1


def test_merge_accepts_a_single_strategy_string_and_the_strategy_alias() -> None:
    merged = merge_candidates(
        [{"code": "600519", "strategies": "value"}, {"code": "000001", "strategy": "growth"}]
    )
    assert [item.strategies for item in merged] == [("value",), ("growth",)]


@pytest.mark.parametrize(
    "raw",
    [
        {"code": "600519", "score": "abc"},
        {"code": "600519", "score": object()},
    ],
)
def test_a_non_numeric_score_raises(raw: dict) -> None:
    with pytest.raises(ValueError, match="不是数值"):
        merge_candidates([raw])


def test_a_boolean_score_is_ignored_rather_than_treated_as_one() -> None:
    assert merge_candidates([{"code": "600519", "score": True}])[0].score is None


def test_non_candidate_input_is_rejected() -> None:
    with pytest.raises(ValueError, match="Candidate 或映射"):
        merge_candidates(["600519"])  # type: ignore[list-item]


def test_as_row_is_json_ready() -> None:
    assert Candidate("600519", ("a",), 0.5, "贵州茅台").as_row() == {
        "code": "600519",
        "strategies": ["a"],
        "score": 0.5,
        "name": "贵州茅台",
    }
