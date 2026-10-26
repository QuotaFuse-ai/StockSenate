"""Fact extraction over recorded TickHub payload shapes, and the quality gate.

These fixtures carry the nested containers the real provider uses
(``indicators``/``income``, ``announcements``, ``flows``, ``history``/
``upcoming``, ``events``, ``reports``). Before the digest read those keys a
healthy payload produced zero facts while its envelope still said ``ok``, and
the gate counted that envelope as evidence.

The regression is pinned from both ends: every recorded domain must yield facts,
and a bundle whose only fact-bearing domains are two of twelve must not score
as complete. No network: fixtures are read from disk, envelopes built in memory.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from stock_senate.data.envelope import (
    Confidence,
    DataEnvelope,
    Freshness,
    SourceStatus,
)
from stock_senate.data.providers.base import envelope_from_payload
from stock_senate.data.router import summarize
from stock_senate.report.render import render_markdown, render_summary
from stock_senate.workflow.digest import _LIST_KEYS, build_digest
from stock_senate.workflow.offline import quality_verdict

FIXTURES = Path(__file__).parent / "fixtures" / "tickhub"
AS_OF = "2026-10-23"
CODE = "600519"
DOMAINS = (
    "kline",
    "quote",
    "financial",
    "announcement",
    "research",
    "risk_event",
    "news",
    "social_buzz",
    "policy_feed",
    "money_flow",
    "dragon_tiger",
    "lockup_calendar",
)


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def envelope_for(domain: str, payload: Any) -> DataEnvelope:
    return envelope_from_payload(
        domain=domain,
        code=CODE,
        payload=payload,
        source="tickhub-cn",
        source_detail="recorded payload",
        data_as_of=AS_OF,
    )


def missing_envelope(domain: str) -> DataEnvelope:
    return DataEnvelope(
        domain=domain,
        code=CODE,
        data_as_of=AS_OF,
        payload=None,
        source="tickhub-cn",
        source_detail="provider returned no rows",
        fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
        freshness=Freshness.ARCHIVE,
        confidence=Confidence.LOW,
        data_source_status=SourceStatus.MISSING,
    )


# --------------------------------------------------------------------------- #
# §2 recorded-shape fact extraction
# --------------------------------------------------------------------------- #

# domain -> (fixture, minimum facts, labels that must be present)
RECORDED = {
    "financial": (
        "financial_indicators.json",
        7,
        {"最新报告期", "营业收入", "净利润", "营收同比", "净利润同比", "ROE", "毛利率"},
    ),
    "announcement": ("announcement.json", 2, {"条目数", "最近条目"}),
    "research": ("research.json", 2, {"条目数", "最近条目"}),
    "risk_event": ("risk_event.json", 3, {"条目数", "最近条目", "高风险条目数"}),
    "money_flow": (
        "money_flow.json",
        3,
        {"最新主力净流入", "最新主力净占比", "近 5 期主力净流入合计"},
    ),
    "lockup_calendar": (
        "lockup_calendar.json",
        4,
        {"最近解禁日", "最近解禁股数（万股）", "最近解禁占比", "解禁记录数"},
    ),
}


@pytest.mark.parametrize("domain", sorted(RECORDED))
def test_recorded_payload_shape_yields_facts(domain: str) -> None:
    name, minimum, labels = RECORDED[domain]
    digest = build_digest({domain: envelope_for(domain, fixture(name))}, as_of=AS_OF)
    entry = digest["domains"][domain]

    assert entry["status"] == "ok"
    assert entry["fact_count"] >= minimum, entry["facts"]
    assert entry["facts"], f"{domain} reported ok but produced no facts"
    present = {fact["label"] for fact in entry["facts"]}
    assert labels <= present, labels - present


def test_recorded_values_survive_extraction() -> None:
    envelopes = {domain: envelope_for(domain, fixture(RECORDED[domain][0])) for domain in RECORDED}
    digest = build_digest(envelopes, as_of=AS_OF)

    financial = {f["label"]: f["value"] for f in digest["domains"]["financial"]["facts"]}
    assert financial["最新报告期"] == "2026年三季报"
    assert financial["营业收入"] == "1231.20 亿"
    assert financial["净利润"] == "608.28 亿"

    money = digest["domains"]["money_flow"]["metrics"]
    assert money["net"] == pytest.approx(2.8e8)
    assert money["net_5d"] == pytest.approx(3.36e8)

    lockup = {f["label"]: f["value"] for f in digest["domains"]["lockup_calendar"]["facts"]}
    assert lockup["最近解禁日"].startswith("2026-11-15")
    assert lockup["解禁记录数"] == "4"

    risk = {f["label"]: f["value"] for f in digest["domains"]["risk_event"]["facts"]}
    assert risk["高风险条目数"] == "1"


def test_income_and_balance_only_payload_still_yields_amounts() -> None:
    """The financial fallback path: no ``indicators``, amounts in ``income``."""
    digest = build_digest(
        {"financial": envelope_for("financial", fixture("financial_income_balance.json"))},
        as_of=AS_OF,
    )
    entry = digest["domains"]["financial"]
    labels = {fact["label"] for fact in entry["facts"]}

    assert {"最新报告期", "营业收入", "净利润"} <= labels
    assert "ROE" not in labels
    assert entry["metrics"]["revenue"] == pytest.approx(1.2312e11)
    assert entry["metrics"]["net_profit"] == pytest.approx(6.0828e10)


def test_nested_containers_read_by_the_digest_are_registered() -> None:
    """A shrink of ``_LIST_KEYS`` must fail here instead of silently zeroing facts."""
    needed = {"announcements", "reports", "flows", "events", "history", "upcoming"}
    assert needed <= set(_LIST_KEYS)


# --------------------------------------------------------------------------- #
# §2 quality gate: evidence is facts, not provider status
# --------------------------------------------------------------------------- #


def _synthetic_digest(statuses: dict[str, tuple[str, int]]) -> dict[str, Any]:
    domains: dict[str, Any] = {}
    for domain, (status, fact_count) in statuses.items():
        facts = [{"label": "条目数", "value": "1"} for _ in range(fact_count)]
        domains[domain] = {"status": status, "facts": facts, "fact_count": fact_count}
    return {"domains": domains, "missing": [], "as_of": AS_OF}


def _real_shaped_bundle() -> tuple[dict[str, Any], dict[str, DataEnvelope]]:
    """7 domains report ok (2 carry facts, 5 are empty), 5 domains are missing.

    This is the shape of the live 600519 run: the old gate counted 7/12 as
    evidence and produced 83.3 + ``complete`` although five of the seven
    "ok" domains had not yielded a single fact.
    """
    envelopes: dict[str, DataEnvelope] = {
        "kline": envelope_for(
            "kline",
            {
                "code": CODE,
                "klines": [
                    {"date": "2026-10-22", "close": 1600.0, "volume": 1_000_000},
                    {"date": "2026-10-23", "close": 1620.9, "volume": 1_200_000},
                ],
            },
        ),
        "money_flow": envelope_for("money_flow", fixture("money_flow.json")),
    }
    for domain in ("announcement", "research", "risk_event", "lockup_calendar", "financial"):
        envelopes[domain] = envelope_for(domain, {"code": CODE, "items": []})
    for domain in ("quote", "news", "social_buzz", "policy_feed", "dragon_tiger"):
        envelopes[domain] = missing_envelope(domain)
    return build_digest(envelopes, as_of=AS_OF), envelopes


def _full_context() -> dict[str, Any]:
    return {
        "analysts": [{"citations": [{"source": "tickhub-cn", "data_as_of": AS_OF}]}],
        "verdict": {"rationale": ["证据一致"]},
        "trade": {"rationale": ["顺势"]},
        "risks": [{"points": ["回撤"]}],
        "threshold": 70.0,
    }


def test_ok_but_empty_domains_do_not_count_as_evidence() -> None:
    digest, envelopes = _real_shaped_bundle()

    assert summarize(envelopes)["ok"] == 7
    fact_bearing = [name for name, entry in digest["domains"].items() if entry["fact_count"] > 0]
    assert fact_bearing == ["kline", "money_flow"]

    verdict = quality_verdict({**_full_context(), "digest": digest})

    assert verdict["dimensions"]["evidence_sufficiency"]["score"] == pytest.approx(16.7)
    assert verdict["partial"] is True
    assert verdict["low_quality"] is True
    assert verdict["score"] < verdict["threshold"], verdict["score"]
    for domain in ("announcement", "research", "risk_event", "lockup_calendar", "financial"):
        assert domain in verdict["notes"]


def test_seven_ok_domains_no_longer_read_as_complete() -> None:
    """Guards the exact regression: 7 ok + 5 empty must not stay 83.3/complete."""
    digest, _ = _real_shaped_bundle()
    verdict = quality_verdict({**_full_context(), "digest": digest})

    assert not (verdict["score"] >= verdict["threshold"] and not verdict["partial"])
    assert verdict["score"] == pytest.approx(66.7), verdict["score"]
    assert "有事实的数据域 2/12" in verdict["notes"]


def test_header_and_footer_agree_on_a_degraded_bundle() -> None:
    digest, envelopes = _real_shaped_bundle()
    verdict = quality_verdict({**_full_context(), "digest": digest})
    summary = summarize(envelopes)
    domains = {
        name: {
            "source": env.source,
            "data_as_of": env.data_as_of,
            "freshness": env.freshness.value,
            "confidence": env.confidence.value,
            "data_source_status": env.data_source_status.value,
            "source_detail": env.source_detail,
        }
        for name, env in envelopes.items()
    }
    state = {
        "run_id": "2026-10-23-600519-test",
        "trace_id": "trace",
        "code": CODE,
        "date": AS_OF,
        "partial": verdict["partial"],
        "decision": {
            "rating": "hold",
            "horizon": "swing",
            "confidence": 55,
            "partial": verdict["partial"],
            "low_quality": verdict["low_quality"],
        },
        "quality": verdict,
        "data": {"summary": summary, "domains": domains, "digest": digest},
    }

    text = render_markdown(state)
    machine = render_summary(state)

    assert summary["status"] == "degraded"
    assert "运行状态：partial" in text
    assert "低于质量阈值，需人工复核" in text
    assert "运行状态：complete" not in text
    assert "数据状态：degraded" in text
    assert machine["partial"] is True
    assert machine["low_quality"] is True
