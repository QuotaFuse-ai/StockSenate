"""Turn data envelopes into a compact, factual digest.

Only values that actually appear in a payload become facts; nothing is
estimated. The digest is the *only* thing the offline engine reads, which is
what keeps ``llm_provider = "offline"`` honest: it can summarise data, it
cannot invent it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from stock_senate.data.envelope import DataEnvelope

POSITIVE_WORDS = (
    "增长",
    "增持",
    "中标",
    "回购",
    "预增",
    "扭亏",
    "利好",
    "突破",
    "创新高",
    "签约",
    "获批",
    "涨",
    "positive",
    "upgrade",
    "beat",
)
NEGATIVE_WORDS = (
    "下滑",
    "亏损",
    "减持",
    "处罚",
    "立案",
    "冻结",
    "退市",
    "违约",
    "下调",
    "风险",
    "诉讼",
    "跌",
    "negative",
    "downgrade",
    "miss",
    "warning",
)

_DATE_KEYS = (
    "date",
    "trade_date",
    "day",
    "time",
    "announcement_date",
    "publish_date",
    "publish_time",
    "report_date",
    "datetime",
)
_TITLE_KEYS = ("title", "headline", "name", "summary", "subject", "content", "text")
# Provider payloads wrap their rows under domain-specific keys; every key a
# supported provider actually uses must be listed or the digest silently sees
# an empty list and reports zero facts from a healthy domain.
_LIST_KEYS = (
    "bars",
    "klines",
    "data",
    "items",
    "list",
    "records",
    "rows",
    "reports",
    "news",
    "announcements",
    "flows",
    "events",
    "history",
    "upcoming",
)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _first_number(row: Mapping[str, Any], keys: Iterable[str]) -> float | None:
    for key in keys:
        if key in row:
            number = _finite(row[key])
            if number is not None:
                return number
    return None


def _first_text(row: Mapping[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return ""


def rows_of(payload: Any) -> list[Mapping[str, Any]]:
    """Find the list of records inside an arbitrary provider payload."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for key in _LIST_KEYS:
            found = payload.get(key)
            if isinstance(found, list):
                return [item for item in found if isinstance(item, Mapping)]
        return [payload]
    return []


def _sentiment(text: str) -> int:
    lowered = text.lower()
    positive = sum(1 for word in POSITIVE_WORDS if word in lowered)
    negative = sum(1 for word in NEGATIVE_WORDS if word in lowered)
    return positive - negative


def _fmt(number: float, digits: int = 2) -> str:
    if number == int(number):
        return str(int(number))
    return f"{number:.{digits}f}"


def _fmt_money(number: float) -> str:
    """Format a yuan amount with a readable unit; the value itself is unchanged."""
    if abs(number) >= 1e8:
        return f"{number / 1e8:.2f} 亿"
    if abs(number) >= 1e4:
        return f"{number / 1e4:.2f} 万"
    return _fmt(number)


def _kline_facts(rows: list[Mapping[str, Any]], as_of: str) -> tuple[list[dict], dict]:
    bars: list[dict[str, Any]] = []
    for row in rows:
        close = _first_number(row, ("close", "收盘", "price", "last"))
        if close is None:
            continue
        bars.append(
            {
                "date": _first_text(row, _DATE_KEYS),
                "close": close,
                "volume": _first_number(row, ("volume", "vol", "成交量")),
            }
        )
    if not bars:
        return [], {}
    bars.sort(key=lambda item: item["date"])
    closes = [bar["close"] for bar in bars]
    volumes = [bar["volume"] for bar in bars if bar["volume"] is not None]
    facts: list[dict] = []
    metrics: dict[str, Any] = {"bars": len(bars)}
    latest = bars[-1]
    facts.append(
        {"label": "最新收盘", "value": f"{_fmt(latest['close'])}（{latest['date'] or '日期未知'}）"}
    )
    if len(closes) >= 2 and closes[0]:
        pct = (closes[-1] / closes[0] - 1) * 100
        metrics["pct"] = pct
        facts.append({"label": f"区间涨跌幅（{len(closes)} 根）", "value": f"{pct:+.2f}%"})
    ma = {}
    for window in (5, 20):
        if len(closes) >= window:
            value = sum(closes[-window:]) / window
            ma[f"ma{window}"] = value
            facts.append({"label": f"MA{window}", "value": _fmt(value)})
    metrics.update(ma)
    if len(volumes) >= 5:
        baseline = sum(volumes[-20:]) / len(volumes[-20:])
        if baseline:
            ratio = volumes[-1] / baseline
            metrics["vol_ratio"] = ratio
            facts.append({"label": "最新量能/近 20 期均量", "value": f"{ratio:.2f}x"})
    metrics["as_of"] = latest["date"] or as_of
    return facts, metrics


def _quote_facts(payload: Mapping[str, Any]) -> tuple[list[dict], dict]:
    row = payload
    facts: list[dict] = []
    metrics: dict[str, Any] = {}
    price = _first_number(row, ("price", "close", "latest", "last", "收盘价", "最新价"))
    if price is not None:
        facts.append({"label": "最新价", "value": _fmt(price)})
    pct = _first_number(row, ("change_pct", "pct_chg", "pct_change", "changePercent", "涨跌幅"))
    if pct is not None:
        metrics["pct"] = pct
        facts.append({"label": "涨跌幅", "value": f"{pct:+.2f}%"})
    turnover = _first_number(row, ("turnover_rate", "turnover", "换手率"))
    if turnover is not None:
        facts.append({"label": "换手率", "value": f"{turnover:.2f}%"})
    amount = _first_number(row, ("amount", "成交额", "turnover_value"))
    if amount is not None:
        facts.append({"label": "成交额", "value": _fmt(amount)})
    return facts, metrics


def _rows_in(payload: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    found = payload.get(key)
    if not isinstance(found, list):
        return []
    return [item for item in found if isinstance(item, Mapping)]


def _money_facts(payload: Mapping[str, Any]) -> tuple[list[dict], dict]:
    """TickHub returns ``flows: [{date, main_net, main_pct, ...}]``."""
    facts: list[dict] = []
    metrics: dict[str, Any] = {}
    rows = _rows_in(payload, "flows")
    if rows:
        # Providers order flows newest first or oldest first; sort by date so
        # "latest" means the latest trading day either way.
        rows.sort(key=lambda row: _first_text(row, _DATE_KEYS))
        latest = rows[-1]
        net = _first_number(
            latest,
            ("main_net", "net_inflow", "main_net_inflow", "net_mf", "净流入", "主力净流入"),
        )
        day = _first_text(latest, _DATE_KEYS)
        if net is not None:
            metrics["net"] = net
            suffix = f"（{day}）" if day else ""
            facts.append({"label": "最新主力净流入", "value": f"{_fmt_money(net)}{suffix}"})
        main_pct = _first_number(latest, ("main_pct", "主力净占比"))
        if main_pct is not None:
            metrics["main_pct"] = main_pct
            facts.append({"label": "最新主力净占比", "value": f"{main_pct:+.2f}%"})
        window = rows[-5:]
        window_values = [
            _first_number(row, ("main_net", "net_inflow", "main_net_inflow", "net_mf"))
            for row in window
        ]
        if len(window) >= 2 and all(value is not None for value in window_values):
            total = float(sum(value for value in window_values if value is not None))
            metrics["net_5d"] = total
            facts.append(
                {"label": f"近 {len(window)} 期主力净流入合计", "value": _fmt_money(total)}
            )
    else:
        net = _first_number(
            payload,
            ("net_inflow", "main_net_inflow", "net_mf", "main_net", "净流入", "主力净流入"),
        )
        if net is not None:
            metrics["net"] = net
            facts.append({"label": "主力净流入", "value": _fmt_money(net)})
    return facts, metrics


def _financial_facts(payload: Mapping[str, Any]) -> tuple[list[dict], dict]:
    """Read TickHub ``indicators`` rows first, then fall back to statement rows.

    ``eastmoney_financial_all`` nests the growth/return ratios under
    ``indicators`` (newest first) and the absolute amounts under ``income``;
    neither lives at the payload top level.
    """
    facts: list[dict] = []
    metrics: dict[str, Any] = {}
    latest: Mapping[str, Any] = {}
    indicators = _rows_in(payload, "indicators")
    if indicators:
        latest = max(
            indicators,
            key=lambda row: _first_text(row, ("report_date", "report_name")),
        )
    if not latest:
        income = _rows_in(payload, "income")
        if income:
            latest = max(income, key=lambda row: _first_text(row, ("report_date", "report_name")))
    source = latest or payload
    period = _first_text(source, ("report_name", "report_date", "REPORT_DATE_NAME"))
    if period:
        facts.append({"label": "最新报告期", "value": period})
    amount_specs: tuple[tuple[str, tuple[str, ...], str], ...] = (
        ("revenue", ("revenue", "total_revenue", "营业收入"), "营业收入"),
        ("net_profit", ("net_profit", "parent_net_profit", "归母净利润", "净利润"), "净利润"),
    )
    for metric, keys, label in amount_specs:
        value = _first_number(source, keys)
        if value is not None:
            metrics[metric] = value
            facts.append({"label": label, "value": _fmt_money(value)})
    ratio_specs: tuple[tuple[str, tuple[str, ...], str], ...] = (
        (
            "revenue_yoy",
            ("revenue_yoy", "revenue_growth", "营业收入同比增长", "营收增速"),
            "营收同比",
        ),
        (
            "profit_yoy",
            ("profit_yoy", "net_profit_yoy", "netprofit_yoy", "净利润同比增长"),
            "净利润同比",
        ),
        ("roe", ("roe", "ROE", "净资产收益率"), "ROE"),
        ("gross_margin", ("gross_margin", "毛利率"), "毛利率"),
    )
    for metric, keys, label in ratio_specs:
        value = _first_number(source, keys)
        if value is not None:
            metrics[metric] = value
            facts.append({"label": label, "value": f"{value:+.2f}%"})
    return facts, metrics


def _items_facts(rows: list[Mapping[str, Any]]) -> tuple[list[dict], dict]:
    items: list[dict[str, str]] = []
    score = 0
    high_severity = 0
    severity_seen = False
    for row in rows[:20]:
        title = _first_text(row, _TITLE_KEYS)
        if not title:
            continue
        title = title[:160]
        items.append({"title": title, "date": _first_text(row, _DATE_KEYS)})
        score += _sentiment(title)
        severity = row.get("severity") or row.get("level")
        if isinstance(severity, str) and severity.strip():
            severity_seen = True
            if severity.strip().lower() in {"high", "严重", "高", "critical"}:
                high_severity += 1
    facts: list[dict] = []
    if items:
        facts.append({"label": "条目数", "value": str(len(items))})
        facts.append({"label": "最近条目", "value": items[0]["title"]})
        if severity_seen:
            facts.append({"label": "高风险条目数", "value": str(high_severity)})
    metrics = {"items": items, "sentiment": score, "count": len(items)}
    if severity_seen:
        metrics["high_severity"] = high_severity
    return facts, metrics


def _lockup_rows(payload: Any) -> list[Mapping[str, Any]]:
    """TickHub splits lockups into ``history`` and ``upcoming``; use both."""
    if isinstance(payload, Mapping):
        rows: list[Mapping[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for key in ("upcoming", "history"):
            for row in _rows_in(payload, key):
                marker = (
                    _first_text(row, ("date", "unlock_date", "解禁日期", "expiry_date")),
                    _first_text(row, ("type", "类型")),
                )
                if marker not in seen:
                    seen.add(marker)
                    rows.append(row)
        if rows:
            return rows
    return rows_of(payload)


def _lockup_facts(rows: list[Mapping[str, Any]], as_of: str) -> tuple[list[dict], dict]:
    entries: list[tuple[date, Mapping[str, Any]]] = []
    for row in rows:
        text = _first_text(row, ("date", "unlock_date", "解禁日期", "expiry_date"))
        try:
            entries.append((date.fromisoformat(text[:10]), row))
        except (TypeError, ValueError):
            continue
    facts: list[dict] = []
    metrics: dict[str, Any] = {"count": len(entries)}
    if not entries:
        return facts, metrics
    try:
        base = date.fromisoformat(as_of[:10])
    except (TypeError, ValueError):
        base = None
    if base is not None:
        upcoming = sorted((day, row) for day, row in entries if day >= base)
        if upcoming:
            nearest_day, nearest_row = upcoming[0]
            days = (nearest_day - base).days
            metrics["days_to_unlock"] = days
            facts.append(
                {"label": "最近解禁日", "value": f"{nearest_day.isoformat()}（{days} 天）"}
            )
            shares = _first_number(nearest_row, ("shares_10k", "able_shares_10k", "解禁股数"))
            if shares is not None:
                facts.append({"label": "最近解禁股数（万股）", "value": _fmt(shares)})
            ratio = _first_number(nearest_row, ("ratio_pct", "占比", "解禁比例"))
            if ratio is not None:
                facts.append({"label": "最近解禁占比", "value": f"{ratio:.2f}%"})
    facts.append({"label": "解禁记录数", "value": str(len(entries))})
    return facts, metrics


def build_digest(envelopes: Mapping[str, DataEnvelope], *, as_of: str) -> dict[str, Any]:
    domains: dict[str, dict[str, Any]] = {}
    for name, envelope in envelopes.items():
        entry: dict[str, Any] = {
            "status": envelope.data_source_status.value,
            "source": envelope.source,
            "source_detail": envelope.source_detail,
            "data_as_of": envelope.data_as_of,
            "freshness": envelope.freshness.value,
            "confidence": envelope.confidence.value,
            "facts": [],
            "fact_count": 0,
            "metrics": {},
            "items": [],
        }
        if envelope.usable:
            payload = envelope.payload
            facts: list[dict] = []
            metrics: dict[str, Any] = {}
            if name == "kline":
                facts, metrics = _kline_facts(rows_of(payload), envelope.as_of_date or as_of)
                entry["items"] = metrics.pop("items", [])
            elif name == "quote" and isinstance(payload, Mapping):
                facts, metrics = _quote_facts(payload)
            elif name == "money_flow" and isinstance(payload, Mapping):
                facts, metrics = _money_facts(payload)
            elif name == "financial" and isinstance(payload, Mapping):
                facts, metrics = _financial_facts(payload)
            elif name == "lockup_calendar":
                facts, metrics = _lockup_facts(_lockup_rows(payload), envelope.as_of_date or as_of)
            else:
                facts, metrics = _items_facts(rows_of(payload))
                entry["items"] = metrics.get("items", [])
            if not facts:
                # A payload shaped differently from the domain-specific schema
                # still counts as evidence when it carries titled records.
                generic, generic_metrics = _items_facts(rows_of(payload))
                if generic:
                    facts = generic
                    entry["items"] = generic_metrics.get("items", [])
                    for key, value in generic_metrics.items():
                        metrics.setdefault(key, value)
            entry["facts"] = facts
            entry["fact_count"] = len(facts)
            entry["metrics"] = metrics
        domains[name] = entry

    missing = [name for name, entry in domains.items() if entry["status"] == "missing"]
    degraded = [name for name, entry in domains.items() if entry["status"] == "degraded"]
    available = [name for name, entry in domains.items() if entry["status"] == "ok"]
    return {
        "as_of": as_of,
        "domains": domains,
        "available": available,
        "degraded": degraded,
        "missing": missing,
    }


__all__ = ["build_digest", "rows_of"]
