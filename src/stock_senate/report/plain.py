"""面向普通人的白话结论层。

``FinalDecision`` 已经有评级、理由、风险和引用，但那些文本是写给专业读者的：
``bearish``、``方向倾向 short``、``置信度 0.38`` 对普通人没有意义。这个模块把
同一批结论翻译成「这是什么 / 为什么 / 风险点」三段，并且守住三条硬约束：

* 每条论断必须挂上本次运行里真实存在的 :class:`~stock_senate.roles.schemas.Citation`
  （来源 + 定位）。找不到证据的论断不会出现在输出里，只会计入
  ``omitted_claims``；找不到证据的整段退化为中性说明。
* 输出先过荐股红线：买卖指令、目标价、收益承诺命中即丢弃该论断并计入
  ``redline_blocked``，绝不改写后保留。
* 置信度原样沿用 ``FinalDecision.confidence``，不新造任何数字。

白话层只做翻译与引用，不产生新事实，因此离线模式与在线模式跑的是同一套逻辑。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from stock_senate.data.envelope import DataEnvelope, SourceStatus
from stock_senate.data.sanitize import compliance_scan
from stock_senate.roles.schemas import (
    AnalystReport,
    Citation,
    FinalDecision,
    PlainClaim,
    PlainConclusion,
    PlainGlossaryEntry,
)

PLAIN_VERSION = "plain-v1"

# rating -> (一句话定位, 白话解释)。措辞刻意回避「买入/卖出」等指令动词。
RATING_PLAIN: dict[str, tuple[str, str]] = {
    "buy": (
        "研究团队整体看好这家公司",
        "这是一条研究评级：团队认为未来一段时间它的表现可能好于市场平均。"
        "「看好」是研究意见，不是让你现在就去交易的指令。",
    ),
    "overweight": (
        "研究团队温和偏看好",
        "比「整体看好」留了更多余地：方向偏正面，但分歧或不确定因素仍然存在。",
    ),
    "hold": (
        "研究团队认为暂时看不出明显偏向",
        "多空理由大体互相抵消，团队没有给出方向性结论；对普通人来说，"
        "这通常意味着「先观察、不急着下判断」。",
    ),
    "underweight": (
        "研究团队温和偏谨慎",
        "方向偏负面，但团队没有给出最谨慎的结论，说明仍有部分因素需要继续跟踪。",
    ),
    "sell": (
        "研究团队整体偏谨慎",
        "团队认为不利因素占上风，对这只股票的看法偏负面。这是研究结论，不是交易指令。",
    ),
    "unknown": (
        "本次没有形成明确方向",
        "材料不足或分歧过大，团队没有给出方向性判断；这本身就是一种结论：先别急着下判断。",
    ),
}

NEUTRAL_WHAT = "本次分析没有取得可以逐条核对的来源，因此只给出中性说明，不给出方向性判断。"
NEUTRAL_WHY = "本次没有找到可以逐条核对的来源，所以不展开理由，避免给出无依据的解释。"
NEUTRAL_RISKS = "本次没有可核验的风险来源；这不等于没有风险，只说明手头材料不足。"

# 领域 -> 普通人能认出的关键词。用于把论断和真实引用对上，不做任何推断。
DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "kline": ("股价", "走势", "均线", "收盘", "涨跌", "成交量", "量能", "MA20", "K线"),
    "quote": ("报价", "行情", "市盈", "市净", "总市值", "换手"),
    "financial": ("财务", "营收", "净利润", "利润", "ROE", "毛利", "现金流", "负债", "业绩"),
    "announcement": ("公告", "披露", "预案", "重组", "回购", "增持计划"),
    "research": ("研报", "机构评级", "一致预期", "券商观点"),
    "risk_event": ("风险事件", "处罚", "立案", "诉讼", "违约", "冻结", "退市"),
    "news": ("新闻", "消息", "媒体报道", "快讯"),
    "social_buzz": ("情绪", "讨论", "热度", "舆情", "看多", "看空"),
    "policy_feed": ("政策", "监管", "补贴", "规划", "行业政策"),
    "money_flow": ("资金", "主力", "净流入", "净流出", "成交额"),
    "dragon_tiger": ("龙虎榜", "游资", "席位"),
    "lockup_calendar": ("解禁", "限售", "解禁日历"),
}

_STANCE_WORDS: dict[str, tuple[str, ...]] = {
    "bullish": ("bullish", "看多", "偏多", "乐观"),
    "bearish": ("bearish", "看空", "偏空", "谨慎"),
    "neutral": ("neutral", "中性", "观望"),
}

# 英文标签 -> 中文白话。先翻译，再判断证据，避免普通人读到生词。
_TERM_PLAIN = {
    "bullish": "偏多",
    "bearish": "偏空",
    "neutral": "中性",
    "unknown": "未明确",
    "long": "偏多",
    "short": "偏空",
    "flat": "没有方向",
    "intraday": "短线",
    "medium": "中期",
}
_TERM_RE = re.compile(r"\b(" + "|".join(sorted(_TERM_PLAIN, key=len, reverse=True)) + r")\b")

GLOSSARY: dict[str, str] = {
    "研究评级": "研究团队给出的综合意见档位，只代表研究观点，不是买卖指令。",
    "置信度": "系统对自己判断的把握程度，0 到 1，越接近 1 越有把握；它不是上涨概率。",
    "持有期": "大致的时间范围，中期通常指几周到几个月，不代表具体买卖时点。",
    "解禁": "原本不能卖的股份到了可以卖的时间，可能增加卖压。",
    "换手率": "一段时间内股票转手的比例，高换手说明交易活跃、分歧可能更大。",
    "MA20": "过去 20 个交易日的平均价格线，常用来观察短期趋势方向。",
    "净流入": "买入金额减去卖出金额；正数说明当天有资金净买入。",
    "量能": "成交量的大小，用来判断价格变化背后有多少资金参与。",
    "ROE": "净资产收益率，衡量公司用股东的钱赚钱的效率，数值越高通常越能赚钱。",
    "数据缺口": "某类数据没拿到或质量不佳，结论因此会更不确定。",
}

# 荐股红线：买卖指令、目标价、收益承诺。命中即整条论断丢弃。
REDLINE_PATTERNS = (
    r"(建议|推荐|应当|应该|可以|可|立即|马上|赶紧|不妨|值得|适合)"
    r"(买入|卖出|加仓|减仓|清仓|抄底|追高|追涨|止盈|止损)",
    r"(买入|卖出|加仓|减仓|清仓)(时机|点位|价位|价格)",
    r"(目标价|目标价位|止盈价|止损位|止损价|建仓价|建仓点位|建仓位置)",
    r"(必涨|必跌|稳赚|包赚|稳赢|保本|保底|翻倍|暴涨|一夜暴富)",
    r"(满仓|梭哈|全仓|重仓)",
    r"(收益|回报|涨幅|盈利|赚)\s*\d+(\.\d+)?\s*(%|％|倍|个点)",
    r"(保证|承诺|确保)[^。！？]{0,10}(收益|盈利|赚钱|不亏)",
    r"\d+(\.\d+)?\s*元",
)
_REDLINE_RE = re.compile("|".join(f"(?:{pattern})" for pattern in REDLINE_PATTERNS))

_MAX_CLAIMS = 5
_MAX_EVIDENCE = 3
_MAX_FLAGS = 5
_MAX_GLOSSARY = 6


def scan_advice(text: str) -> list[str]:
    """Return the 荐股红线 fragments found in ``text`` (empty means clean)."""
    if not text:
        return []
    return [match.group(0) for match in _REDLINE_RE.finditer(str(text))]


def _humanize(text: str) -> str:
    """Translate role-output shorthand into plain Chinese and tidy whitespace."""
    if not text:
        return ""
    converted = _TERM_RE.sub(lambda match: _TERM_PLAIN[match.group(1)], str(text))
    converted = re.sub(r"\s+", " ", converted).strip()
    return converted


def _sentence(text: str) -> str:
    return text if text.endswith(("。", "！", "？")) else f"{text}。"


def _dedupe(citations: Sequence[Citation]) -> list[Citation]:
    seen: set[tuple[str, str, str, str]] = set()
    unique: list[Citation] = []
    for citation in citations:
        key = (citation.domain, citation.source, citation.data_as_of, citation.detail)
        if key in seen:
            continue
        seen.add(key)
        unique.append(citation)
    return unique


def _envelope_citation(envelope: DataEnvelope) -> Citation:
    return Citation(
        domain=envelope.domain,
        source=envelope.source,
        data_as_of=envelope.data_as_of,
        detail=envelope.source_detail or str(envelope.freshness),
    )


class _EvidenceIndex:
    """Every citation this run actually produced, indexed for matching."""

    def __init__(
        self,
        reports: Sequence[AnalystReport],
        envelopes: Mapping[str, DataEnvelope],
        digest: Mapping[str, Any] | None,
    ) -> None:
        self.reports = [report for report in reports if report is not None]
        self.by_domain: dict[str, Citation] = {}
        for report in self.reports:
            for citation in report.citations:
                self.by_domain.setdefault(citation.domain, citation)
        for name, envelope in envelopes.items():
            if envelope.data_source_status is SourceStatus.MISSING:
                continue
            self.by_domain.setdefault(name, _envelope_citation(envelope))
        self.facts: dict[str, list[str]] = {}
        domains = dict((digest or {}).get("domains") or {})
        for name, entry in domains.items():
            if not isinstance(entry, Mapping) or name not in self.by_domain:
                continue
            values = [
                str(fact.get("value") or "").strip()
                for fact in entry.get("facts") or []
                if isinstance(fact, Mapping)
            ]
            self.facts[name] = [value for value in values if len(value) >= 3]

    def _from_report(self, report: AnalystReport) -> list[Citation]:
        if report.citations:
            return list(report.citations)
        domains = {fact.domain for fact in report.facts}
        return [self.by_domain[domain] for domain in sorted(domains) if domain in self.by_domain]

    def for_claim(self, text: str) -> list[Citation]:
        """Citations that genuinely support ``text``; empty means unsupported.

        The tiers are applied in order and the first hit wins, so a claim is
        never padded with citations from unrelated sections:
        named analyst → quoted fact → domain keyword → matching stance.
        """
        lowered = text.lower()
        named: list[Citation] = []
        for report in self.reports:
            names = (report.display_name, report.role)
            if any(name and name in text for name in names):
                named.extend(self._from_report(report))
        if named:
            return _dedupe(named)[:_MAX_EVIDENCE]

        quoted: list[Citation] = []
        for domain, values in self.facts.items():
            if any(value in text or value.lower() in lowered for value in values):
                quoted.append(self.by_domain[domain])
        if quoted:
            return _dedupe(quoted)[:_MAX_EVIDENCE]

        keywords: list[Citation] = []
        for domain, terms in DOMAIN_KEYWORDS.items():
            if domain not in self.by_domain:
                continue
            if any(term in text for term in terms):
                keywords.append(self.by_domain[domain])
        if keywords:
            return _dedupe(keywords)[:_MAX_EVIDENCE]

        stances: list[Citation] = []
        for stance, words in _STANCE_WORDS.items():
            if not any(word in lowered for word in words):
                continue
            for report in self.reports:
                if report.stance == stance:
                    stances.extend(self._from_report(report))
        return _dedupe(stances)[:_MAX_EVIDENCE]


def _risk_flags(
    envelopes: Mapping[str, DataEnvelope],
    *,
    low_quality: bool,
    partial: bool,
) -> list[str]:
    flags: list[str] = []
    for name in sorted(envelopes):
        envelope = envelopes[name]
        if envelope.data_source_status is SourceStatus.MISSING:
            flags.append(f"数据缺口：{name} 缺失（{envelope.source_detail or '无可用来源'}）")
        elif envelope.data_source_status is SourceStatus.DEGRADED:
            flags.append(f"数据质量降级：{name}（{envelope.source_detail or '来源置信度低'}）")
    if low_quality:
        flags.append("质量闸门认为本次材料质量偏低，结论需要人工复核。")
    if partial:
        flags.append("本次运行有环节未完整完成，结论只覆盖可用部分。")
    if len(flags) > _MAX_FLAGS:
        extra = len(flags) - _MAX_FLAGS + 1
        flags = flags[: _MAX_FLAGS - 1] + [f"另有 {extra} 项数据或质量提示。"]
    return flags


def _glossary(text: str) -> list[PlainGlossaryEntry]:
    entries = [
        PlainGlossaryEntry(term=term, note=note) for term, note in GLOSSARY.items() if term in text
    ]
    return entries[:_MAX_GLOSSARY]


def build_plain_conclusion(
    decision: FinalDecision,
    *,
    analysts: Sequence[AnalystReport] = (),
    envelopes: Mapping[str, DataEnvelope] | None = None,
    digest: Mapping[str, Any] | None = None,
    low_quality: bool | None = None,
    partial: bool = False,
) -> PlainConclusion:
    """Translate a validated decision into what / why / risks, evidence first.

    ``low_quality`` overrides ``decision.low_quality`` so the caller can pass the
    quality gate's verdict without mutating the decision first.
    """
    envelope_map = dict(envelopes or {})
    index = _EvidenceIndex(analysts, envelope_map, digest)
    omitted = 0
    blocked = 0
    degraded = False

    def _supported(raw: str) -> tuple[PlainClaim | None, bool]:
        """Return ``(claim, redline_hit)``; ``None`` claim means unsupported."""
        nonlocal omitted, blocked, degraded
        text = _humanize(raw)
        if not text:
            return None, False
        if scan_advice(text) or compliance_scan(text):
            blocked += 1
            degraded = True
            return None, True
        evidence = index.for_claim(text)
        if not evidence:
            omitted += 1
            degraded = True
            return None, False
        return PlainClaim(text=_sentence(text), evidence=evidence), False

    why: list[PlainClaim] = []
    seen: set[str] = set()
    for reason in decision.reasons:
        claim, _ = _supported(str(reason or ""))
        if claim is None or claim.text in seen:
            continue
        seen.add(claim.text)
        why.append(claim)
        if len(why) >= _MAX_CLAIMS:
            break

    risks: list[PlainClaim] = []
    for risk in decision.risks:
        claim, _ = _supported(str(risk or ""))
        if claim is None or claim.text in seen:
            continue
        seen.add(claim.text)
        risks.append(claim)
        if len(risks) >= _MAX_CLAIMS:
            break

    rating_label, rating_meaning = RATING_PLAIN.get(decision.rating, RATING_PLAIN["unknown"])
    if decision.rating != "unknown" and "研究评级" not in rating_meaning:
        rating_meaning = f"{rating_meaning}这只是研究评级，代表团队的研究观点，不是买卖指令。"
    what_text = _humanize(f"{rating_label}。{rating_meaning}")
    what_evidence = _dedupe([citation for claim in why for citation in claim.evidence])[
        :_MAX_EVIDENCE
    ]
    if scan_advice(what_text) or compliance_scan(what_text):
        what_text = NEUTRAL_WHAT
        what_evidence = []
        blocked += 1
        degraded = True
    if what_evidence:
        what = PlainClaim(text=what_text, evidence=what_evidence)
    else:
        what = PlainClaim(text=NEUTRAL_WHAT, neutral=True)
        degraded = True

    if not why:
        why = [PlainClaim(text=NEUTRAL_WHY, neutral=True)]
    if not risks:
        risks = [PlainClaim(text=NEUTRAL_RISKS, neutral=True)]

    confidence = float(decision.confidence)
    confidence_note = (
        f"整体置信度 {confidence:.2f}（0~1，越高表示系统越有把握，但它不是上涨概率）；"
        "该数值直接沿用投资经理终审结果，没有另行计算。"
    )
    effective_low_quality = bool(decision.low_quality) if low_quality is None else bool(low_quality)
    if effective_low_quality:
        confidence_note += "质量闸门认为本次材料质量偏低，结论需要人工复核。"

    analysed_text = " ".join(
        [
            what.text,
            confidence_note,
            *(claim.text for claim in why),
            *(claim.text for claim in risks),
        ]
    )
    return PlainConclusion(
        version=PLAIN_VERSION,
        what=what,
        why=why,
        risks=risks,
        confidence=confidence,
        confidence_note=confidence_note,
        glossary=_glossary(analysed_text),
        risk_flags=_risk_flags(
            envelope_map, low_quality=effective_low_quality, partial=bool(partial)
        ),
        degraded=degraded,
        omitted_claims=omitted,
        redline_blocked=blocked,
    )


__all__ = [
    "DOMAIN_KEYWORDS",
    "GLOSSARY",
    "NEUTRAL_RISKS",
    "NEUTRAL_WHAT",
    "NEUTRAL_WHY",
    "PLAIN_VERSION",
    "RATING_PLAIN",
    "REDLINE_PATTERNS",
    "build_plain_conclusion",
    "scan_advice",
]
