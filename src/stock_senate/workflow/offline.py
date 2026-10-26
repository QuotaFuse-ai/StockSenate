"""Deterministic offline engine.

``llm_provider = "offline"`` is a first-class mode: every role output is
produced by transparent rules over the factual digest. It exists so the whole
pipeline can run in CI, in an air-gapped install, and during acceptance without
a model key. It never reads anything except the digest, so it cannot fabricate
numbers.

The same contracts are used online: a remote model returns JSON that is
validated against the exact same Pydantic models.
"""

from __future__ import annotations

from typing import Any, Mapping

ANALYST_ROLE_HINTS = {
    "market": "价格与量能",
    "social": "市场情绪",
    "news": "新闻事件",
    "fundamentals": "财务基本面",
    "policy": "政策环境",
    "hot_money": "资金行为",
    "lockup": "解禁供给",
}


def _domain(ctx: Mapping[str, Any], name: str) -> dict[str, Any]:
    return ((ctx.get("digest") or {}).get("domains") or {}).get(name) or {}


def _role_domains(ctx: Mapping[str, Any]) -> list[str]:
    return list(ctx.get("role_domains") or [])


def _facts_for(ctx: Mapping[str, Any], domains: list[str], limit: int = 8) -> list[dict[str, str]]:
    facts: list[dict[str, str]] = []
    for domain in domains:
        entry = _domain(ctx, domain)
        if entry.get("status") == "missing":
            continue
        for fact in (entry.get("facts") or [])[:4]:
            facts.append(
                {
                    "label": str(fact.get("label", "")),
                    "value": str(fact.get("value", "")),
                    "domain": domain,
                    "data_as_of": str(entry.get("data_as_of") or ""),
                }
            )
            if len(facts) >= limit:
                return facts
    return facts


def _citations_for(ctx: Mapping[str, Any], domains: list[str]) -> list[dict[str, str]]:
    citations: list[dict[str, str]] = []
    for domain in domains:
        entry = _domain(ctx, domain)
        if entry.get("status") == "missing":
            continue
        citations.append(
            {
                "domain": domain,
                "source": str(entry.get("source") or "unknown"),
                "data_as_of": str(entry.get("data_as_of") or ""),
                "detail": str(entry.get("source_detail") or entry.get("freshness") or ""),
            }
        )
    return citations


def _gaps_for(ctx: Mapping[str, Any], domains: list[str]) -> list[str]:
    gaps: list[str] = []
    for domain in domains:
        entry = _domain(ctx, domain)
        status = entry.get("status")
        if status == "missing":
            gaps.append(f"{domain} 数据缺失：{entry.get('source_detail') or '无可用来源'}")
        elif status == "degraded":
            gaps.append(f"{domain} 数据降级：{entry.get('source_detail') or '来源置信度低'}")
    return gaps


def _metrics(ctx: Mapping[str, Any], domain: str) -> dict[str, Any]:
    return dict(_domain(ctx, domain).get("metrics") or {})


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _stance_from_score(score: float, *, threshold: float = 0.5) -> str:
    if score >= threshold:
        return "bullish"
    if score <= -threshold:
        return "bearish"
    return "neutral"


def _analyst_stance(role: str, ctx: Mapping[str, Any]) -> tuple[str, float, list[str]]:
    """Return ``(stance, confidence, notes)`` using only digest facts."""
    notes: list[str] = []
    score = 0.0
    evidence = 0

    if role == "market" or role == "trader":
        kline = _metrics(ctx, "kline")
        quote = _metrics(ctx, "quote")
        pct = kline.get("pct", quote.get("pct"))
        if isinstance(pct, (int, float)):
            score += 1.0 if pct > 0 else -1.0 if pct < 0 else 0.0
            evidence += 1
            notes.append(f"区间涨跌幅 {pct:+.2f}%")
        if isinstance(kline.get("ma20"), (int, float)) and isinstance(kline.get("bars"), int):
            last = _last_close(ctx)
            if last is not None:
                above = last >= kline["ma20"]
                score += 0.5 if above else -0.5
                evidence += 1
                notes.append("收盘价位于 MA20 上方" if above else "收盘价位于 MA20 下方")
        ratio = kline.get("vol_ratio")
        if isinstance(ratio, (int, float)) and ratio > 1.5:
            direction = 1 if score >= 0 else -1
            score += 0.3 * direction
            notes.append(f"量能为近 20 期均量的 {ratio:.2f} 倍")
    elif role == "fundamentals":
        fin = _metrics(ctx, "financial")
        if isinstance(fin.get("profit_yoy"), (int, float)):
            score += 1.0 if fin["profit_yoy"] > 0 else -1.0
            evidence += 1
            notes.append(f"净利润同比 {fin['profit_yoy']:+.2f}%")
        if isinstance(fin.get("revenue_yoy"), (int, float)):
            score += 0.5 if fin["revenue_yoy"] > 0 else -0.5
            evidence += 1
            notes.append(f"营收同比 {fin['revenue_yoy']:+.2f}%")
        if isinstance(fin.get("roe"), (int, float)):
            score += 0.5 if fin["roe"] > 10 else -0.3 if fin["roe"] < 0 else 0.0
            notes.append(f"ROE {fin['roe']:+.2f}%")
    elif role in {"social", "news", "policy"}:
        domain = {"social": "social_buzz", "news": "news", "policy": "policy_feed"}[role]
        meta = _metrics(ctx, domain)
        sentiment = meta.get("sentiment", 0)
        count = meta.get("count", 0)
        if count:
            evidence += 1
            score += _clamp(sentiment / max(3, count) * 3, -2.0, 2.0)
            notes.append(f"{domain} 条目 {count} 条，情绪净值 {sentiment}")
    elif role == "hot_money":
        flow = _metrics(ctx, "money_flow")
        if isinstance(flow.get("net"), (int, float)):
            evidence += 1
            score += 1.0 if flow["net"] > 0 else -1.0
            notes.append(f"主力净流入 {flow['net']:+.2f}")
        dragon = _metrics(ctx, "dragon_tiger")
        if dragon.get("count"):
            evidence += 1
            score += _clamp(dragon.get("sentiment", 0) / max(3, dragon["count"]) * 2, -1.0, 1.0)
    elif role == "lockup":
        lockup = _metrics(ctx, "lockup_calendar")
        days = lockup.get("days_to_unlock")
        if isinstance(days, int):
            evidence += 1
            if days <= 30:
                score -= 1.0
                notes.append(f"距最近解禁 {days} 天")
            else:
                notes.append(f"距最近解禁 {days} 天，短期供给压力有限")

    usable = [name for name in _role_domains(ctx) if _domain(ctx, name).get("status") != "missing"]
    if evidence == 0:
        # A role with tools but no usable data says so instead of guessing.
        return "unknown", 0.05, ["可用数据不足，未形成方向判断"]

    stance = _stance_from_score(score)
    confidence = _clamp(0.25 + 0.08 * len(usable) + 0.15 * min(abs(score), 2.0), 0.1, 0.85)
    return stance, confidence, notes


def _last_close(ctx: Mapping[str, Any]) -> float | None:
    kline = _metrics(ctx, "kline")
    for key in ("last_close", "close"):
        value = kline.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    entry = _domain(ctx, "kline")
    for fact in entry.get("facts") or []:
        if fact.get("label") == "最新收盘":
            text = str(fact.get("value", "")).split("（")[0]
            try:
                return float(text)
            except ValueError:
                return None
    return None


def analyst_report(ctx: Mapping[str, Any]) -> dict[str, Any]:
    role = str(ctx.get("role") or "market")
    display = str(ctx.get("display_name") or role)
    domains = _role_domains(ctx) or ["kline"]
    stance, confidence, notes = _analyst_stance(role, ctx)
    facts = _facts_for(ctx, domains)
    gaps = _gaps_for(ctx, domains)
    hint = ANALYST_ROLE_HINTS.get(role, "该角色关注的数据")
    if facts:
        lead = "、".join(f"{item['label']} {item['value']}" for item in facts[:2])
        summary = (
            f"{display}从{hint}角度给出 {stance} 判断（置信度 {confidence:.2f}）。"
            f"关键事实：{lead}。以上数值均直接取自数据载荷，未做模型推断。"
        )
    else:
        summary = (
            f"{display}从{hint}角度未能取得可用数据，因而不给出方向判断。"
            "系统按缺失数据处理，等待下一次数据刷新。"
        )
    key_points = [f"事实：{item['label']} = {item['value']}" for item in facts[:4]]
    key_points += [f"判断：{note}" for note in notes[:2]]
    if gaps:
        key_points.append("数据完整性存在缺口，结论仅覆盖可用部分。")
    return {
        "role": role,
        "display_name": display,
        "partial": stance == "unknown" and bool(gaps),
        "notes": "offline deterministic engine",
        "stance": stance,
        "confidence": round(confidence, 4),
        "summary": summary,
        "key_points": key_points,
        "facts": facts,
        "citations": _citations_for(ctx, domains),
        "data_gaps": gaps,
    }


def _analyst_sides(ctx: Mapping[str, Any]) -> tuple[list[dict], list[dict]]:
    reports = list(ctx.get("analysts") or [])
    bull = [item for item in reports if item.get("stance") == "bullish"]
    bear = [item for item in reports if item.get("stance") == "bearish"]
    return bull, bear


def research_argument(ctx: Mapping[str, Any]) -> dict[str, Any]:
    side = str(ctx.get("side") or "bull")
    bull, bear = _analyst_sides(ctx)
    supporters, opponents = (bull, bear) if side == "bull" else (bear, bull)
    points: list[str] = []
    for report in supporters[:4]:
        for item in (report.get("key_points") or [])[:2]:
            points.append(f"{report.get('display_name') or report.get('role')}：{item}")
    if not points:
        points.append("暂无同向分析师结论；本方仅能基于中性事实陈述，不额外引申。")
    rebuttals: list[str] = []
    for report in opponents[:3]:
        for item in (report.get("key_points") or [])[:1]:
            rebuttals.append(
                f"对方关于「{item}」的论述尚未获得全部数据域验证，需保留保留意见。"
            )
    if not rebuttals:
        rebuttals.append("对方未给出可核验的反向证据。")
    total = max(1, len(bull) + len(bear))
    confidence = _clamp((len(supporters) + 0.5) / (total + 1), 0.05, 0.9)
    return {
        "role": "bull" if side == "bull" else "bear",
        "side": side,
        "points": points[:6],
        "rebuttals": rebuttals[:4],
        "confidence": round(confidence, 4),
        "notes": "offline deterministic engine",
    }


def research_verdict(ctx: Mapping[str, Any]) -> dict[str, Any]:
    reports = list(ctx.get("analysts") or [])
    weights = dict(ctx.get("weights") or {})
    if not reports:
        return {
            "role": "research_manager",
            "partial": True,
            "stance": "unknown",
            "confidence": 0.0,
            "rationale": ["没有启用的分析师，无法形成研究结论。"],
            "unresolved": ["需要至少启用一个分析师角色"],
            "rounds_used": int(ctx.get("rounds_used") or 0),
            "convergence_reason": "no_analysts",
            "notes": "offline deterministic engine",
        }
    total_weight = 0.0
    score = 0.0
    for report in reports:
        weight = float(weights.get(str(report.get("role")), 1.0))
        stance = report.get("stance")
        value = {"bullish": 1.0, "bearish": -1.0}.get(str(stance), 0.0)
        confidence = float(report.get("confidence") or 0.0)
        total_weight += weight * max(0.2, confidence)
        score += weight * value * max(0.2, confidence)
    normalized = score / total_weight if total_weight else 0.0
    stance = _stance_from_score(normalized, threshold=0.15)
    confidence = _clamp(abs(normalized) * 0.9 + 0.15, 0.05, 0.9)
    rationale = [
        f"{report.get('display_name') or report.get('role')}：{report.get('stance')}"
        for report in reports[:6]
    ]
    rationale.append(f"加权方向分 {normalized:+.3f}，据此给出 {stance}。")
    unresolved: list[str] = []
    for report in reports:
        unresolved.extend(list(report.get("data_gaps") or [])[:1])
    return {
        "role": "research_manager",
        "partial": False,
        "stance": stance,
        "confidence": round(confidence, 4),
        "rationale": rationale,
        "unresolved": list(dict.fromkeys(unresolved))[:6],
        "rounds_used": int(ctx.get("rounds_used") or 0),
        "convergence_reason": str(ctx.get("convergence_reason") or "max_rounds"),
        "notes": "offline deterministic engine",
    }


def trade_plan(ctx: Mapping[str, Any]) -> dict[str, Any]:
    verdict = dict(ctx.get("verdict") or {})
    stance = verdict.get("stance")
    direction = {"bullish": "long", "bearish": "short", "neutral": "flat"}.get(str(stance), "unknown")
    confidence = float(verdict.get("confidence") or 0.0)
    risks = list(verdict.get("unresolved") or [])[:4]
    if not risks:
        risks = ["数据缺口未完全覆盖，结论对新增事件敏感。"]
    return {
        "role": "trader",
        "direction": direction,
        "horizon": "medium",
        "rationale": [
            f"研究结论为 {stance}，交易员仅描述方向倾向，不给出任何价格或仓位安排。",
            f"方向置信度 {confidence:.2f}，在市场结构未变化前维持该倾向。",
        ],
        "risks": risks,
        "confidence": round(_clamp(confidence * 0.9, 0.0, 0.9), 4),
        "notes": "offline deterministic engine",
    }


def risk_argument(ctx: Mapping[str, Any]) -> dict[str, Any]:
    side = str(ctx.get("side") or "neutral")
    verdict = dict(ctx.get("verdict") or {})
    trade = dict(ctx.get("trade") or {})
    stance = verdict.get("stance", "unknown")
    unresolved = list(verdict.get("unresolved") or [])
    trade_risks = list(trade.get("risks") or [])
    points = [f"关注：{item}" for item in (unresolved + trade_risks)[:4]]
    if not points:
        points = ["当前数据缺口有限，风险主要来自事件冲击。"]
    severity = {"aggressive": "low", "neutral": "mid", "conservative": "high"}[side]
    if unresolved:
        severity = "high" if side == "conservative" or len(unresolved) >= 3 else severity
    label = {"aggressive": "激进", "neutral": "中立", "conservative": "保守"}[side]
    points.insert(0, f"{label}视角：研究结论为 {stance}，需按该视角衡量可承受的不确定性。")
    return {
        "role": f"risk_{side}",
        "side": side,
        "stance": stance if stance in {"bullish", "bearish", "neutral"} else "unknown",
        "points": points[:5],
        "severity": severity,
        "notes": "offline deterministic engine",
    }


def final_decision(ctx: Mapping[str, Any]) -> dict[str, Any]:
    verdict = dict(ctx.get("verdict") or {})
    trade = dict(ctx.get("trade") or {})
    risks = list(ctx.get("risks") or [])
    stance = str(verdict.get("stance") or "unknown")
    confidence = float(verdict.get("confidence") or 0.0)
    if stance == "bullish":
        rating = "overweight" if confidence >= 0.6 else "hold"
    elif stance == "bearish":
        rating = "underweight" if confidence >= 0.6 else "hold"
    elif stance == "neutral":
        rating = "hold"
    else:
        rating = "unknown"
    reasons = list(verdict.get("rationale") or [])[:3]
    reasons.append(
        f"方向倾向 {trade.get('direction', 'unknown')}，持有期 {trade.get('horizon', 'unknown')}；"
        "仅作研究结论，不构成交易指令。"
    )
    risk_points: list[str] = []
    for item in risks:
        risk_points.extend(list(item.get("points") or [])[:1])
    if not risk_points:
        risk_points = list(verdict.get("unresolved") or [])[:3] or ["缺乏反向证据，结论稳定性待验证。"]
    partial = stance == "unknown" or bool(verdict.get("partial"))
    return {
        "role": "portfolio_manager",
        "partial": partial,
        "rating": rating,
        "horizon": trade.get("horizon", "unknown"),
        "reasons": reasons,
        "risks": risk_points[:5],
        "confidence": round(_clamp(confidence, 0.0, 0.9), 4),
        "low_quality": False,
        "hitl": dict(ctx.get("hitl") or {}),
        "notes": "offline deterministic engine",
    }


def quality_verdict(ctx: Mapping[str, Any]) -> dict[str, Any]:
    digest = ctx.get("digest") or {}
    domains = list(digest.get("domains") or {})
    usable = [name for name in domains if (digest["domains"][name] or {}).get("status") != "missing"]
    missing = list(digest.get("missing") or [])
    evidence = 100.0 * len(usable) / max(1, len(domains))

    citations = 0
    valid = 0
    for report in ctx.get("analysts") or []:
        for citation in report.get("citations") or []:
            citations += 1
            if citation.get("source") and citation.get("data_as_of"):
                valid += 1
    citation_score = 100.0 * valid / citations if citations else 0.0

    coherence = 100.0
    if not ctx.get("analysts"):
        coherence -= 40
    if not (ctx.get("verdict") or {}).get("rationale"):
        coherence -= 20
    if not (ctx.get("trade") or {}).get("rationale"):
        coherence -= 20
    if not (ctx.get("risks") or []):
        coherence -= 20
    coherence = max(0.0, coherence)

    score = round(0.4 * evidence + 0.3 * citation_score + 0.3 * coherence, 1)
    threshold = float(ctx.get("threshold") or 70.0)
    notes = [f"可用数据域 {len(usable)}/{len(domains)}，质量分 {score:.1f}。"]
    if missing:
        notes.append("缺失数据域：" + "、".join(missing))
    if score < threshold:
        notes.append(f"低于阈值 {threshold:.0f}，标记为 low_quality 并提示人工复核。")
    return {
        "role": "quality_gate",
        "partial": False,
        "score": score,
        "low_quality": score < threshold,
        "threshold": threshold,
        "dimensions": {
            "logic_coherence": {"score": coherence, "note": "分析—研究—交易—风险—终审链条完整度"},
            "evidence_sufficiency": {"score": round(evidence, 1), "note": "可用数据域占比"},
            "citation_accuracy": {"score": round(citation_score, 1), "note": "可追溯引用的比例"},
        },
        "notes": " ".join(notes),
    }


_GENERATORS = {
    "AnalystReport": analyst_report,
    "ResearchArgument": research_argument,
    "ResearchVerdict": research_verdict,
    "TradePlan": trade_plan,
    "RiskArgument": risk_argument,
    "FinalDecision": final_decision,
    "QualityVerdict": quality_verdict,
}


def generate(schema_name: str, context: Mapping[str, Any]) -> dict[str, Any]:
    generator = _GENERATORS.get(schema_name)
    if generator is None:
        raise ValueError(f"离线引擎不支持输出 Schema: {schema_name}")
    return generator(context)


__all__ = ["generate"]
