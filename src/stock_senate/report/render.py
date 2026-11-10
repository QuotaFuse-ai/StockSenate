"""Markdown rendering of one saved run.

The renderer reads nothing but the structured state, so a snapshot can be
re-rendered months later without calling a model or a provider. Every free-text
fragment passes the sentence-level compliance filter: a level or order phrase
that somehow reached a role output can never reach a report file.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from stock_senate.data.sanitize import compliance_filter

STANCE_LABEL = {
    "bullish": "看多",
    "bearish": "看空",
    "neutral": "中性",
    "unknown": "未知",
}
RATING_LABEL = {
    "buy": "买入",
    "overweight": "增持",
    "hold": "持有",
    "underweight": "减持",
    "sell": "卖出",
    "unknown": "未知",
}
DIRECTION_LABEL = {"long": "偏多", "short": "偏空", "flat": "无方向", "unknown": "未知"}
SEVERITY_LABEL = {"high": "高", "mid": "中", "low": "低", "unknown": "未知"}

_DEFAULT_SECTIONS = [
    ("plain", "白话结论"),
    ("analysts", "一、分析师观点"),
    ("research", "二、多空辩论与研究经理结论"),
    ("trade", "三、交易方案"),
    ("risk", "四、风险辩论"),
    ("decision", "五、投资经理终审"),
    ("quality", "六、质量评估"),
    ("data", "附：数据来源与新鲜度"),
]


def _safe(text: Any) -> str:
    return compliance_filter(str(text or "")).replace("\n", " ").strip()


def _bullets(lines: list[str], *, limit: int = 20) -> list[str]:
    return [f"- {_safe(line)}" for line in lines if str(line or "").strip()][:limit]


def _table(header: list[str], rows: list[list[Any]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(_safe(cell) for cell in row) + " |")
    return out


def _section_titles(template: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    sections = dict((template or {}).get("sections") or {})
    if not sections:
        return list(_DEFAULT_SECTIONS)
    titles = []
    for key, default in _DEFAULT_SECTIONS:
        entry = sections.get(key) or {}
        titles.append((key, str(entry.get("title") or default)))
    return titles


def render_summary(state: Mapping[str, Any]) -> dict[str, Any]:
    """The machine-readable ``summary.json`` body."""
    decision = dict(state.get("decision") or {})
    data = dict(state.get("data") or {}).get("summary") or {}
    quality = dict(state.get("quality") or {})
    return {
        "run_id": state.get("run_id"),
        "trace_id": state.get("trace_id"),
        "code": state.get("code"),
        "date": state.get("date"),
        "generated_at": state.get("generated_at"),
        "llm_provider": state.get("llm_provider"),
        "config_hash": state.get("config_hash"),
        "rating": decision.get("rating"),
        "action": decision.get("action"),
        "horizon": decision.get("horizon"),
        "confidence": decision.get("confidence"),
        "partial": bool(state.get("partial") or decision.get("partial")),
        "low_quality": bool(decision.get("low_quality")),
        "quality_score": quality.get("score"),
        "quality_threshold": quality.get("threshold"),
        "plain": decision.get("plain"),
        "data": data,
        "skipped": list(decision.get("skipped") or []),
        "budget": dict(state.get("budget") or {}),
        "hitl": dict(state.get("hitl") or {}),
    }


def render_markdown(
    state: Mapping[str, Any],
    template: Mapping[str, Any] | None = None,
    *,
    compact: bool = False,
) -> str:
    """Render the full report, or the concise conclusion when ``compact``."""
    code = state.get("code", "")
    date = state.get("date", "")
    decision = dict(state.get("decision") or {})
    meta = dict((template or {}).get("meta") or {})
    titles = dict(_section_titles(template))
    lines: list[str] = []

    title = str(meta.get("title") or "{code} {name} 投研合议报告").format(code=code, name="")
    lines.append(f"# {title.strip()}")
    lines.append("")
    lines.append(f"- 标的：`{code}` · 分析日期：`{date}` · 运行：`{state.get('run_id', '')}`")
    lines.append(
        f"- 研究评级：**{RATING_LABEL.get(str(decision.get('rating')), '未知')}**"
        f" · 持有期：{decision.get('horizon', 'unknown')}"
        f" · 置信度：{decision.get('confidence', 0)}"
    )
    status = "partial" if state.get("partial") else "complete"
    flag = " · **低于质量阈值，需人工复核**" if decision.get("low_quality") else ""
    lines.append(f"- 运行状态：{status}{flag} · trace：`{state.get('trace_id', '')}`")
    lines.append("")

    # The plain-language conclusion comes first: it is the section a reader
    # without finance background needs before any role detail.
    lines.extend(_render_plain(state, titles["plain"]))
    if not compact:
        lines.extend(_render_analysts(state, titles["analysts"]))
    lines.extend(_render_research(state, titles["research"], compact=compact))
    lines.extend(_render_trade(state, titles["trade"]))
    if not compact:
        lines.extend(_render_risk(state, titles["risk"]))
    lines.extend(_render_decision(state, titles["decision"]))
    if not compact:
        lines.extend(_render_quality(state, titles["quality"]))
    lines.extend(_render_data(state, titles["data"]))

    disclaimer = dict((template or {}).get("disclaimer") or {})
    text = str(
        disclaimer.get("text") or "本报告由 AI 自动生成，仅用于研究与教学，不构成任何投资建议。"
    ).strip()
    includes = meta.get("include_disclaimer", True)
    if includes:
        lines.append("")
        lines.append("---")
        lines.append("")
        for paragraph in [part.strip() for part in text.splitlines() if part.strip()]:
            lines.append(_safe(paragraph))
    lines.append("")
    return "\n".join(lines)


def _evidence_line(evidence: list[Any]) -> str:
    parts = []
    for citation in evidence:
        if not isinstance(citation, Mapping):
            continue
        where = " · ".join(
            str(citation.get(key) or "")
            for key in ("domain", "source", "data_as_of", "detail")
            if str(citation.get(key) or "").strip()
        )
        if where:
            parts.append(f"`{where}`")
    return "；".join(parts)


def _render_plain(state: Mapping[str, Any], title: str) -> list[str]:
    decision = dict(state.get("decision") or {})
    plain = decision.get("plain")
    if not isinstance(plain, Mapping) or not plain:
        return []
    out = [f"## {title}", ""]

    what = dict(plain.get("what") or {})
    out.append(f"**这是什么**：{_safe(what.get('text'))}")
    what_evidence = [item for item in what.get("evidence") or [] if isinstance(item, Mapping)]
    out.append(
        "  - 依据：" + _evidence_line(what_evidence)
        if what_evidence
        else "  - 依据：无（中性说明，不做方向判断）"
    )
    out.append("")

    out.append("**为什么**")
    out.extend(_plain_claims(plain.get("why")))
    out.append("")
    out.append("**风险点**")
    out.extend(_plain_claims(plain.get("risks")))
    out.append("")

    if plain.get("confidence_note"):
        out.append(f"- {_safe(plain['confidence_note'])}")
    flags = [str(flag) for flag in plain.get("risk_flags") or [] if str(flag).strip()]
    if flags:
        out.append("")
        out.append("**风险与数据提示**")
        out.extend(_bullets(flags, limit=8))
    glossary = [item for item in plain.get("glossary") or [] if isinstance(item, Mapping)]
    if glossary:
        out.append("")
        out.append("**名词小抄**")
        out.extend(
            f"- **{_safe(item.get('term'))}**：{_safe(item.get('note'))}" for item in glossary[:6]
        )
    dropped = int(plain.get("omitted_claims") or 0)
    blocked = int(plain.get("redline_blocked") or 0)
    if dropped or blocked:
        notes = []
        if dropped:
            notes.append(f"省略 {dropped} 条没有证据支撑的论断")
        if blocked:
            notes.append(f"拦截 {blocked} 条触碰荐股红线的表述")
        out.append("")
        out.append("- 合规降级：" + "；".join(notes) + "。")
    out.append("")
    return out


def _plain_claims(claims: Any) -> list[str]:
    lines: list[str] = []
    for claim in claims or []:
        if not isinstance(claim, Mapping):
            continue
        lines.append(f"- {_safe(claim.get('text'))}")
        evidence = [item for item in claim.get("evidence") or [] if isinstance(item, Mapping)]
        if evidence:
            lines.append("  - 依据：" + _evidence_line(evidence))
    return lines or ["- 本次没有可核验的来源，因此不展开。"]


def _render_analysts(state: Mapping[str, Any], title: str) -> list[str]:
    reports = list(state.get("analysts") or [])
    if not reports:
        return []
    out = [f"## {title}", ""]
    for report in reports:
        name = report.get("display_name") or report.get("role")
        partial = "（partial）" if report.get("partial") else ""
        out.append(f"### {name}{partial}")
        out.append(
            f"- 立场：{STANCE_LABEL.get(str(report.get('stance')), '未知')}"
            f" · 置信度：{report.get('confidence', 0)}"
        )
        if report.get("summary"):
            out.append(f"- 摘要：{_safe(report['summary'])}")
        points = list(report.get("key_points") or [])
        if points:
            out.append("")
            out.extend(_bullets(points, limit=6))
        facts = list(report.get("facts") or [])
        if facts:
            out.append("")
            rows = [
                [
                    fact.get("label"),
                    fact.get("value"),
                    fact.get("domain"),
                    fact.get("data_as_of"),
                ]
                for fact in facts[:10]
            ]
            out.extend(_table(["指标", "数值", "数据域", "数据日期"], rows))
        gaps = list(report.get("data_gaps") or [])
        if gaps:
            out.append("")
            out.append("数据缺口：" + "；".join(_safe(gap) for gap in gaps[:5]))
        out.append("")
    return out


def _render_research(state: Mapping[str, Any], title: str, *, compact: bool) -> list[str]:
    research = dict(state.get("research") or {})
    if not research:
        return []
    debate = dict((state.get("debate") or {}).get("bull_bear") or {})
    out = [f"## {title}", ""]
    if not compact and debate:
        out.append(
            f"- 辩论轮次：{debate.get('rounds_used', 0)}"
            f" · 收敛：{debate.get('convergence_reason', '')}"
            f" · 新增论点占比：{debate.get('new_point_ratio', 0)}"
        )
        by_side: dict[str, list[str]] = {"bull": [], "bear": []}
        for argument in debate.get("arguments") or []:
            side = str(argument.get("side") or "")
            if side in by_side:
                by_side[side].extend(list(argument.get("points") or []))
        for side, label in (("bull", "多头"), ("bear", "空头")):
            if by_side[side]:
                out.append("")
                out.append(f"**{label}要点**")
                out.extend(_bullets(by_side[side], limit=8))
        out.append("")
    out.append(
        f"- 研究经理结论：{STANCE_LABEL.get(str(research.get('stance')), '未知')}"
        f" · 置信度：{research.get('confidence', 0)}"
        f" · 轮次：{research.get('rounds_used', 0)}"
    )
    rationale = list(research.get("rationale") or [])
    if rationale:
        out.extend(_bullets(rationale, limit=8))
    unresolved = list(research.get("unresolved") or [])
    if unresolved:
        out.append("")
        out.append("未决问题：" + "；".join(_safe(item) for item in unresolved[:5]))
    out.append("")
    return out


def _render_trade(state: Mapping[str, Any], title: str) -> list[str]:
    trade = dict(state.get("trade") or {})
    if not trade:
        return []
    out = [f"## {title}", ""]
    out.append(
        f"- 方向倾向：{DIRECTION_LABEL.get(str(trade.get('direction')), '未知')}"
        f" · 持有期：{trade.get('horizon', 'unknown')}"
        f" · 置信度：{trade.get('confidence', 0)}"
    )
    out.append("- 只给出方向倾向与研究理由，不包含任何可执行价位或仓位安排。")
    rationale = list(trade.get("rationale") or [])
    if rationale:
        out.append("")
        out.extend(_bullets(rationale, limit=8))
    risks = list(trade.get("risks") or [])
    if risks:
        out.append("")
        out.append("**方案风险**")
        out.extend(_bullets(risks, limit=8))
    out.append("")
    return out


def _render_risk(state: Mapping[str, Any], title: str) -> list[str]:
    arguments = list((state.get("risk") or {}).get("arguments") or [])
    if not arguments:
        return []
    out = [f"## {title}", ""]
    labels = {"aggressive": "激进", "neutral": "中立", "conservative": "保守"}
    for argument in arguments:
        side = str(argument.get("side") or "")
        out.append(
            f"### {labels.get(side, side)}风险辩手"
            f"（严重度：{SEVERITY_LABEL.get(str(argument.get('severity')), '未知')}）"
        )
        points = list(argument.get("points") or [])
        if points:
            out.extend(_bullets(points, limit=8))
        out.append("")
    return out


def _render_decision(state: Mapping[str, Any], title: str) -> list[str]:
    decision = dict(state.get("decision") or {})
    if not decision:
        return []
    out = [f"## {title}", ""]
    out.append(f"- 研究评级：**{RATING_LABEL.get(str(decision.get('rating')), '未知')}**")
    out.append(
        f"- 持有期：{decision.get('horizon', 'unknown')} · 置信度：{decision.get('confidence', 0)}"
    )
    if decision.get("low_quality"):
        out.append("- 质量闸门：低于阈值，需人工复核。")
    reasons = list(decision.get("reasons") or [])
    if reasons:
        out.append("")
        out.append("**结论与理由**")
        out.extend(_bullets(reasons, limit=8))
    risks = list(decision.get("risks") or [])
    if risks:
        out.append("")
        out.append("**主要风险**")
        out.extend(_bullets(risks, limit=8))
    hitl = dict(decision.get("hitl") or {})
    traces = list(hitl.get("traces") or [])
    if traces:
        out.append("")
        out.append("**人工介入记录**")
        for trace in traces:
            out.append(
                f"- {trace.get('checkpoint')}：{trace.get('decision')}"
                f"{'（自动放行）' if trace.get('auto_approved') else ''}"
            )
    out.append("")
    return out


def _render_quality(state: Mapping[str, Any], title: str) -> list[str]:
    quality = dict(state.get("quality") or {})
    if not quality:
        return []
    out = [f"## {title}", ""]
    mode = str(quality.get("mode") or "rules")
    suffix = "" if mode == "rules" else f" · 评分方式 {mode}"
    out.append(
        f"- 质量分：{quality.get('score', 0)} / 阈值 {quality.get('threshold', 0)}"
        f"{' · low_quality' if quality.get('low_quality') else ''}{suffix}"
    )
    dimensions = dict(quality.get("dimensions") or {})
    if dimensions:
        rows = [
            [name, item.get("score"), item.get("note")]
            for name, item in dimensions.items()
            if isinstance(item, Mapping)
        ]
        out.append("")
        out.extend(_table(["维度", "分数", "说明"], rows))
    defects = [item for item in quality.get("defects") or [] if isinstance(item, Mapping)]
    if defects:
        rows = [
            [item.get("dimension"), item.get("location"), item.get("issue"), item.get("fix_hint")]
            for item in defects
        ]
        out.append("")
        out.extend(_table(["维度", "位置", "问题", "修复建议"], rows))
    if quality.get("notes"):
        out.append("")
        out.append(_safe(quality["notes"]))
    out.append("")
    return out


def _render_data(state: Mapping[str, Any], title: str) -> list[str]:
    data = dict(state.get("data") or {})
    domains = dict(data.get("domains") or {})
    if not domains:
        return []
    out = [f"## {title}", ""]
    summary = dict(data.get("summary") or {})
    out.append(
        f"- 数据状态：{summary.get('status', 'missing')}"
        f" · ok {summary.get('ok', 0)} / degraded {summary.get('degraded', 0)}"
        f" / missing {summary.get('missing', 0)}"
    )
    digest_domains = dict((data.get("digest") or {}).get("domains") or {})
    rows = []
    for domain, envelope in sorted(domains.items()):
        digest_entry = dict(digest_domains.get(domain) or {})
        rows.append(
            [
                domain,
                envelope.get("source"),
                envelope.get("data_as_of"),
                envelope.get("freshness"),
                envelope.get("confidence"),
                envelope.get("data_source_status"),
                digest_entry.get("fact_count", len(digest_entry.get("facts") or [])),
                envelope.get("source_detail"),
            ]
        )
    out.append("")
    out.extend(
        _table(
            ["数据域", "来源", "数据日期", "新鲜度", "置信度", "状态", "事实条数", "说明"],
            rows,
        )
    )
    out.append("")
    return out


__all__ = ["RATING_LABEL", "render_markdown", "render_summary"]
