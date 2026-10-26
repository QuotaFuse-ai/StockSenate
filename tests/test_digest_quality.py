"""Digest fact extraction and the fact-based quality gate.

The gate used to count a domain as covered whenever its provider reported
``ok``, so a payload whose rows the digest could not parse still scored as
evidence. These tests pin both halves of the fix: provider-shaped nested lists
produce facts, and coverage is measured on extracted facts rather than on the
provider's status string.

No network: envelopes are constructed in memory from recorded payload shapes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from stock_senate.data.envelope import (
    Confidence,
    DataEnvelope,
    Freshness,
    SourceStatus,
)
from stock_senate.workflow.digest import build_digest
from stock_senate.workflow.offline import quality_verdict

AS_OF = "2026-10-23"


def envelope(domain: str, payload: object, status: SourceStatus = SourceStatus.OK) -> DataEnvelope:
    return DataEnvelope(
        domain=domain,
        code="600519",
        data_as_of=AS_OF,
        payload=payload,
        source="fixture",
        source_detail="recorded provider payload",
        fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
        freshness=Freshness.ARCHIVE,
        confidence=Confidence.MEDIUM,
        data_source_status=status,
    )


def test_nested_provider_lists_produce_facts() -> None:
    envelopes = {
        "announcement": envelope(
            "announcement",
            {
                "code": "600519",
                "announcements": [{"title": "2026 年第三季度报告", "date": "2026-10-20"}],
                "total": 1,
            },
        ),
        "risk_event": envelope(
            "risk_event",
            {
                "code": "600519",
                "events": [{"title": "股东减持计划", "date": "2026-10-21", "severity": "high"}],
                "total": 1,
            },
        ),
        "lockup_calendar": envelope(
            "lockup_calendar",
            {
                "code": "600519",
                "history": [],
                "upcoming": [
                    {"date": "2026-11-15", "shares_10k": 120.0, "ratio_pct": 1.2, "type": "定增"}
                ],
            },
        ),
        "money_flow": envelope(
            "money_flow",
            {
                "code": "600519",
                "flows": [
                    {"date": "2026-10-22", "main_net": 1.2e8, "main_pct": 3.1},
                    {"date": "2026-10-23", "main_net": 2.8e8, "main_pct": 3.7},
                ],
            },
        ),
    }

    digest = build_digest(envelopes, as_of=AS_OF)

    for domain in envelopes:
        entry = digest["domains"][domain]
        assert entry["status"] == "ok"
        assert entry["fact_count"] >= 1, f"{domain} reported ok but yielded no facts"
        assert entry["facts"], f"{domain} facts list must not be empty"


def test_financial_nested_sections_produce_facts() -> None:
    digest = build_digest(
        {
            "financial": envelope(
                "financial",
                {
                    "code": "600519",
                    "indicators": [
                        {
                            "report_date": "2026-09-30",
                            "revenue": 9.2e10,
                            "net_profit": 4.4e10,
                            "roe": 16.75,
                            "gross_margin": 89.5,
                        }
                    ],
                    "income": [
                        {"report_date": "2026-09-30", "revenue": 9.2e10, "net_profit": 4.4e10}
                    ],
                },
            )
        },
        as_of=AS_OF,
    )

    entry = digest["domains"]["financial"]
    assert entry["fact_count"] >= 4
    labels = {fact["label"] for fact in entry["facts"]}
    assert {"营业收入", "净利润", "ROE"} <= labels


def _digest_with(domains_with_facts: list[str], ok_without_facts: list[str]) -> dict:
    entries = {}
    for domain in domains_with_facts:
        entries[domain] = {
            "status": "ok",
            "facts": [{"label": "条目数", "value": "1"}],
            "fact_count": 1,
        }
    for domain in ok_without_facts:
        entries[domain] = {"status": "ok", "facts": [], "fact_count": 0}
    return {"domains": entries, "missing": [], "as_of": AS_OF}


def test_quality_gate_scores_facts_not_status() -> None:
    with_facts = ["kline", "financial", "research"]
    empty = ["announcement", "risk_event", "lockup_calendar"]
    verdict = quality_verdict({"digest": _digest_with(with_facts, empty), "threshold": 70.0})

    assert verdict["dimensions"]["evidence_sufficiency"]["score"] == 50.0
    assert verdict["partial"] is True
    assert verdict["low_quality"] is True
    assert "announcement" in verdict["notes"]


def test_quality_gate_clean_when_every_domain_has_facts() -> None:
    analysts = [{"citations": [{"source": "eastmoney", "data_as_of": AS_OF}]}]
    verdict = quality_verdict(
        {
            "digest": _digest_with(["kline", "financial", "research"], []),
            "threshold": 70.0,
            "analysts": analysts,
            "verdict": {"rationale": ["证据一致"]},
            "trade": {"rationale": ["顺势"]},
            "risks": [{"points": ["回撤"]}],
        }
    )

    assert verdict["dimensions"]["evidence_sufficiency"]["score"] == 100.0
    assert verdict["partial"] is False
    assert verdict["low_quality"] is False
