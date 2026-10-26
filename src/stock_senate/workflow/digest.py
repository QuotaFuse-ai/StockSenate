"""Turn data envelopes into a compact, factual digest.

Only values that actually appear in a payload become facts; nothing is
estimated. The digest is the *only* thing the offline engine reads, which is
what keeps ``llm_provider = "offline"`` honest: it can summarise data, it
cannot invent it.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Iterable, Mapping

from stock_senate.data.envelope import DataEnvelope

POSITIVE_WORDS = (
    "增长", "增持", "中标", "回购", "预增", "扭亏", "利好", "突破", "创新高",
    "签约", "获批", "涨", "positive", "upgrade", "beat",
)
NEGATIVE_WORDS = (
    "下滑", "亏损", "减持", "处罚", "立案", "冻结", "退市", "违约", "下调",
    "风险", "诉讼", "跌", "negative", "downgrade", "miss", "warning",
)

_DATE_KEYS = ("date", "trade_date", "day", "time", "announcement_date", "publish_date", "datetime")
_TITLE_KEYS = ("title", "headline", "name", "summary", "subject", "content", "text")
_LIST_KEYS = ("bars", "klines", "data", "items", "list", "records", "rows", "reports", "news")


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
    facts.append({"label": "最新收盘", "value": f"{_fmt(latest['close'])}（{latest['date'] or '日期未知'}）"})
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


def _money_facts(payload: Mapping[str, Any]) -> tuple[list[dict], dict]:
    net = _first_number(
        payload,
        ("net_inflow", "main_net_inflow", "net_mf", "main_net", "净流入", "主力净流入"),
    )
    facts: list[dict] = []
    metrics: dict[str, Any] = {}
    if net is not None:
        metrics["net"] = net
        facts.append({"label": "主力净流入", "value": _fmt(net)})
    return facts, metrics


def _financial_facts(payload: Mapping[str, Any]) -> tuple[list[dict], dict]:
    mapping = {
        "revenue_yoy": ("revenue_yoy", "revenue_growth", "营业收入同比增长", "营收增速"),
        "profit_yoy": ("net_profit_yoy", "profit_yoy", "netprofit_yoy", "净利润同比增长"),
        "roe": ("roe", "ROE", "净资产收益率"),
        "gross_margin": ("gross_margin", "毛利率"),
    }
    facts: list[dict] = []
    metrics: dict[str, Any] = {}
    for metric, keys in mapping.items():
        value = _first_number(payload, keys)
        if value is not None:
            metrics[metric] = value
            label = {
                "revenue_yoy": "营收同比",
                "profit_yoy": "净利润同比",
                "roe": "ROE",
                "gross_margin": "毛利率",
            }[metric]
            suffix = "%" if metric != "roe" or True else ""
            facts.append({"label": label, "value": f"{value:+.2f}{suffix}"})
    return facts, metrics


def _items_facts(rows: list[Mapping[str, Any]]) -> tuple[list[dict], dict]:
    items: list[dict[str, str]] = []
    score = 0
    for row in rows[:20]:
        title = _first_text(row, _TITLE_KEYS)
        if not title:
            continue
        title = title[:160]
        items.append({"title": title, "date": _first_text(row, _DATE_KEYS)})
        score += _sentiment(title)
    facts: list[dict] = []
    if items:
        facts.append({"label": "条目数", "value": str(len(items))})
        facts.append({"label": "最近条目", "value": items[0]["title"]})
    return facts, {"items": items, "sentiment": score, "count": len(items)}


def _lockup_facts(rows: list[Mapping[str, Any]], as_of: str) -> tuple[list[dict], dict]:
    dates: list[date] = []
    for row in rows:
        text = _first_text(row, ("date", "unlock_date", "解禁日期", "expiry_date"))
        try:
            dates.append(date.fromisoformat(text[:10]))
        except (TypeError, ValueError):
            continue
    facts: list[dict] = []
    metrics: dict[str, Any] = {"count": len(dates)}
    if dates:
        base = date.fromisoformat(as_of[:10])
        upcoming = sorted(day for day in dates if day >= base)
        if upcoming:
            days = (upcoming[0] - base).days
            metrics["days_to_unlock"] = days
            facts.append({"label": "最近解禁日", "value": f"{upcoming[0].isoformat()}（{days} 天）"})
        facts.append({"label": "解禁记录数", "value": str(len(dates))})
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
                facts, metrics = _lockup_facts(rows_of(payload), envelope.as_of_date or as_of)
            else:
                facts, metrics = _items_facts(rows_of(payload))
                entry["items"] = metrics.get("items", [])
            entry["facts"] = facts
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
