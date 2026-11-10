"""白话结论层：三段结构、强制证据、荐股红线与三处载荷透传。

QUOT-536 的验收点全部落在这里：固定辩论输入下输出「这是什么 / 为什么 /
风险点」三段、每条论断挂 Citation、无证据的论断不得出现、荐股表述被拦截，
以及 plain 字段在 decision / summary.json / markdown / MCP 中一致。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from stock_senate.data.envelope import Confidence, DataEnvelope, Freshness, SourceStatus
from stock_senate.data.sanitize import compliance_scan
from stock_senate.graph.senate_graph import SenateGraph
from stock_senate.mcp_server import tools
from stock_senate.report import store
from stock_senate.report.plain import build_plain_conclusion, scan_advice
from stock_senate.report.render import render_markdown, render_summary
from stock_senate.roles.schemas import (
    AnalystReport,
    Citation,
    FinalDecision,
    PlainClaim,
)

CODE = "600519"
DAY = "2026-01-28"


@pytest.fixture()
def offline_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, dict]:
    from cli_support import write_archive, write_config_dir

    archive = write_archive(tmp_path / "archive")
    config_dir = write_config_dir(tmp_path / "config", archive)
    home = tmp_path / "home"
    monkeypatch.setenv("STOCKSENATE_HOME", str(home))
    monkeypatch.setenv("STOCKSENATE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("STOCKSENATE_LLM_PROVIDER", "offline")
    monkeypatch.setenv("STOCKSENATE_DATA_MODE", "local")
    graph = SenateGraph(config=config_dir)
    state, decision = graph.propagate(CODE, DAY, hitl=False)
    return state, decision


def _envelope(domain: str, status: SourceStatus = SourceStatus.OK) -> DataEnvelope:
    return DataEnvelope(
        domain=domain,
        code=CODE,
        data_as_of=DAY,
        payload={},
        source="tickhub",
        source_detail=f"{domain} 来源说明",
        fetched_at="2026-01-28T15:00:00+08:00",
        freshness=Freshness.ARCHIVE,
        confidence=Confidence.HIGH,
        data_source_status=status,
    )


def _analyst() -> AnalystReport:
    return AnalystReport(
        role="market",
        display_name="市场分析师",
        stance="bearish",
        confidence=0.7,
        summary="股价跌破均线",
        citations=[
            Citation(
                domain="kline",
                source="tickhub",
                data_as_of=DAY,
                detail="kline/600519/2026-01-28",
            )
        ],
    )


def _decision(**extra) -> FinalDecision:
    payload = {
        "role": "portfolio_manager",
        "rating": "underweight",
        "horizon": "medium",
        "confidence": 0.42,
        "reasons": ["市场分析师：bearish"],
        "risks": ["关注：价格跌破 MA20"],
    }
    payload.update(extra)
    return FinalDecision(**payload)


def _texts(plain) -> list[str]:
    claims = [plain.what, *plain.why, *plain.risks]
    return [claim.text for claim in claims] + [plain.confidence_note]


# --------------------------------------------------------------------------- #
# 三段结构与逐段引用
# --------------------------------------------------------------------------- #


def test_three_sections_carry_evidence_and_reuse_confidence() -> None:
    plain = build_plain_conclusion(
        _decision(),
        analysts=[_analyst()],
        envelopes={"kline": _envelope("kline")},
    )

    assert plain.version == "plain-v1"
    assert plain.what.text and plain.what.evidence
    assert plain.why and all(claim.evidence for claim in plain.why)
    assert plain.risks and all(claim.evidence for claim in plain.risks)
    # 置信度沿用终审，不新造数字。
    assert plain.confidence == 0.42
    assert "0.42" in plain.confidence_note
    # 英文标签被翻成中文，术语给出白话注解。
    dumped = json.dumps(plain.model_dump(), ensure_ascii=False)
    assert "bearish" not in dumped
    assert "偏空" in dumped
    assert any(entry.term == "置信度" for entry in plain.glossary)
    assert plain.degraded is False
    assert plain.omitted_claims == 0


def test_evidence_only_comes_from_real_citations() -> None:
    plain = build_plain_conclusion(
        _decision(), analysts=[_analyst()], envelopes={"kline": _envelope("kline")}
    )
    evidence = [citation for claim in plain.why for citation in claim.evidence]
    assert evidence
    assert {(item.domain, item.source) for item in evidence} == {("kline", "tickhub")}


# --------------------------------------------------------------------------- #
# 无证据论断不得出现
# --------------------------------------------------------------------------- #


def test_claim_without_citation_never_reaches_the_output() -> None:
    decision = _decision(
        reasons=["外星人收购了这家公司", "市场分析师：bearish"],
        risks=["来自未来的内幕消息"],
    )
    plain = build_plain_conclusion(
        decision, analysts=[_analyst()], envelopes={"kline": _envelope("kline")}
    )

    dumped = json.dumps(plain.model_dump(), ensure_ascii=False)
    assert "外星人" not in dumped
    assert "内幕消息" not in dumped
    assert plain.omitted_claims == 2
    assert plain.degraded is True
    # 风险段没有证据时退化为中性说明，而不是空着或编造。
    assert plain.risks[0].neutral is True
    assert plain.risks[0].evidence == []


def test_no_evidence_at_all_degrades_what_to_neutral() -> None:
    decision = FinalDecision(
        role="portfolio_manager",
        rating="hold",
        confidence=0.3,
        reasons=["没有任何来源可以核对的判断"],
        risks=[],
    )
    plain = build_plain_conclusion(decision)

    assert plain.what.neutral is True
    assert plain.what.evidence == []
    assert plain.why[0].neutral is True
    assert plain.degraded is True


def test_schema_rejects_an_unsupported_claim() -> None:
    with pytest.raises(ValidationError):
        PlainClaim(text="没有证据的论断")


# --------------------------------------------------------------------------- #
# 荐股红线
# --------------------------------------------------------------------------- #


def test_redline_blocks_trade_instructions_and_price_targets() -> None:
    decision = _decision(
        reasons=[
            "建议买入，目标价 20 元",
            "市场分析师：bearish",
            "目标收益 30%",
        ],
        risks=["必涨，满仓买入"],
    )
    plain = build_plain_conclusion(
        decision, analysts=[_analyst()], envelopes={"kline": _envelope("kline")}
    )

    dumped = json.dumps(plain.model_dump(), ensure_ascii=False)
    for banned in ("建议买入", "目标价", "满仓", "必涨", "30%"):
        assert banned not in dumped
    assert plain.redline_blocked == 3
    assert plain.degraded is True
    # 合规的那条理由仍然保留。
    assert any("偏空" in claim.text for claim in plain.why)


def test_every_emitted_text_is_compliance_clean() -> None:
    plain = build_plain_conclusion(
        _decision(
            reasons=["市场分析师：bearish", "建议卖出"],
            risks=["目标价 12 元", "价格跌破 MA20 需要留意"],
        ),
        analysts=[_analyst()],
        envelopes={"kline": _envelope("kline")},
    )
    for text in _texts(plain):
        assert compliance_scan(text) == []
        assert scan_advice(text) == []
    for entry in plain.glossary:
        assert compliance_scan(entry.note) == []
        assert scan_advice(entry.note) == []


# --------------------------------------------------------------------------- #
# 固定演练输入 → 三处载荷
# --------------------------------------------------------------------------- #


def test_offline_run_exposes_plain_in_decision_record_and_markdown(
    offline_run: tuple[dict, dict],
) -> None:
    state, decision = offline_run
    plain = decision["plain"]

    assert plain["version"] == "plain-v1"
    assert plain["what"]["text"]
    assert plain["why"] and plain["risks"]
    assert plain["confidence"] == decision["confidence"]
    claims = [plain["what"], *plain["why"], *plain["risks"]]
    for claim in claims:
        assert "evidence" in claim
        if not claim["neutral"]:
            assert claim["evidence"], "每条论断都必须挂证据"

    # summary.json 与 markdown 都带上白话层。
    summary = render_summary(state)
    assert summary["plain"] == plain
    markdown = render_markdown(state)
    assert "## 白话结论" in markdown
    assert markdown.index("## 白话结论") < markdown.index("## 一、分析师观点")
    for text in _texts_from_dict(plain):
        assert compliance_scan(text) == []
        assert scan_advice(text) == []

    saved = store.load(decision["run_id"])
    assert saved["decision"]["plain"] == plain


def test_mcp_surfaces_pass_plain_through(offline_run: tuple[dict, dict]) -> None:
    _state, decision = offline_run

    report = tools.report_query(run_id=decision["run_id"])
    assert report["ok"] is True
    assert report["summary"]["plain"] == decision["plain"]
    assert report["sections"]["plain"] == decision["plain"]

    analysed = tools.analyse(CODE, DAY, hitl=False)
    assert analysed["ok"] is True
    assert analysed["plain"] == analysed["decision"]["plain"]
    assert analysed["plain"]["what"]["text"]


def _texts_from_dict(plain: dict) -> list[str]:
    claims = [plain["what"], *plain["why"], *plain["risks"]]
    return [claim["text"] for claim in claims] + [plain["confidence_note"]]
