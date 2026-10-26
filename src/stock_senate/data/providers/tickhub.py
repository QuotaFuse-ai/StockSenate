"""TickHub-CN provider.

TickHub is the only permitted path to live A-share market data (DEC-002); this
module never talks to an upstream vendor directly. Two channels are supported:

* in-process import (``prefer_local_import = true``) — no subprocess, default;
* stdio MCP (``transport = "stdio"``) — delegated to TickHub's own client when
  the package is importable, so StockSenate still owns no transport code.

Quote data is realtime by construction. A historical analysis therefore fails
the router's no-lookahead check instead of silently using today's price, which
is the honest outcome: there is no archived quote to read.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from stock_senate.data.envelope import date_only
from stock_senate.data.providers.base import (
    Provider,
    ProviderNotAvailable,
    envelope_from_payload,
)

# domain -> ordered tool candidates. The first tool that returns data wins.
DOMAIN_TOOLS: dict[str, tuple[str, ...]] = {
    "kline": ("tencent_kline",),
    "quote": ("tencent_quote",),
    "financial": ("eastmoney_financial_all", "eastmoney_finance_indicators"),
    "announcement": ("cninfo_announcements",),
    "research": ("eastmoney_reports", "sina_research_reports"),
    "money_flow": ("stock_fund_flow",),
    "dragon_tiger": ("dragon_tiger",),
    "lockup_calendar": ("lockup_expiry",),
    "risk_event": ("cninfo_announcement_risk",),
    "news": ("eastmoney_stock_news",),
    "social_buzz": ("eastmoney_stock_news",),
    "policy_feed": ("cls_telegraph", "cctv_news"),
}

_CONFIDENCE = {"A": "high", "B": "medium", "C": "low"}

# Domains whose upstream data changes only when a filing lands. They use
# ``announcement_lookback_days`` (default 180) rather than the 60-day quote window.
_SEMI_STATIC_DOMAINS = {"announcement", "risk_event"}

_DATE_KEYS = (
    "date",
    "publish_date",
    "publish_time",
    "announcement_time",
    "notice_date",
    "report_date",
    "trade_date",
    "time",
    "datetime",
)


class TickHubProvider(Provider):
    kind = "mcp"

    def fetch(
        self, domain: str, code: str, as_of: str, params: dict[str, Any] | None = None
    ) -> Any:
        client = self._client()
        tools = DOMAIN_TOOLS.get(domain)
        if not tools:
            raise ProviderNotAvailable(f"tickhub: no tool mapped for domain {domain}")

        params = dict(params or {})
        failures: list[str] = []
        for tool in tools:
            try:
                call_params = self._params(tool, domain, code, as_of, params)
                raw = client.call(tool, call_params)
            except Exception as exc:  # noqa: BLE001 - provider boundary
                failures.append(f"{tool}: {exc}")
                continue
            envelope = self._envelope(domain, code, as_of, tool, raw)
            if envelope is not None:
                return envelope
            failures.append(f"{tool}: empty payload")
        raise ProviderNotAvailable("tickhub: " + "; ".join(failures or ["no candidate tool"]))

    # ------------------------------------------------------------------ #
    # client / params
    # ------------------------------------------------------------------ #

    def _client(self) -> Any:
        if not hasattr(self, "_client_instance"):
            package = str(self.config.get("package") or "tickhub-cn").replace("-", "_")
            try:
                module = __import__(package, fromlist=["StockDataClient"])
                factory = module.StockDataClient
            except (ImportError, AttributeError) as exc:
                raise ProviderNotAvailable(
                    f"{package} 未安装；`pip install stocksenate[stockdata]` 后可用"
                ) from exc
            self._client_instance = factory()
        return self._client_instance

    def _params(
        self, tool: str, domain: str, code: str, as_of: str, extra: dict[str, Any]
    ) -> dict[str, Any]:
        lookback = int(extra.get("lookback_days") or self.config.get("lookback_days") or 60)
        if domain in _SEMI_STATIC_DOMAINS:
            # Filings cluster around report deadlines; a 60-day window can miss
            # every announcement for a quiet quarter, so the digest would score
            # an "ok" domain with zero facts. Semi-static domains get a wider
            # window from the same provider config.
            lookback = int(
                extra.get("lookback_days") or self.config.get("announcement_lookback_days") or 180
            )
        start = _shift(as_of, -lookback)
        base: dict[str, Any] = {"code": code}
        if tool == "tencent_kline":
            base.update(
                {
                    "period": "daily",
                    "adjust": "qfq",
                    "start": start,
                    "end": as_of,
                    "limit": int(extra.get("limit") or 320),
                }
            )
        elif tool == "tencent_quote":
            base = {"codes": code}
        elif tool == "eastmoney_financial_all":
            base["periods"] = int(extra.get("periods") or 4)
        elif tool == "cninfo_announcements":
            base.update({"start": start, "end": as_of, "limit": int(extra.get("limit") or 30)})
        elif tool == "cninfo_announcement_risk":
            base.update({"start": start, "end": as_of, "limit": int(extra.get("limit") or 100)})
        elif tool in {"eastmoney_reports", "sina_research_reports"}:
            base.update({"page_size": int(extra.get("limit") or 20)})
        elif tool == "stock_fund_flow":
            base["days"] = int(extra.get("days") or lookback)
        elif tool == "dragon_tiger":
            base["date"] = as_of
            base["page_size"] = int(extra.get("limit") or 100)
        elif tool == "lockup_expiry":
            base.update({"trade_date": as_of, "forward_days": int(extra.get("forward_days") or 90)})
        elif tool == "eastmoney_finance_indicators":
            base["code"] = code
        elif tool == "eastmoney_stock_news":
            base.update({"code": code, "limit": int(extra.get("limit") or 20)})
        elif tool == "cls_telegraph":
            base = {"limit": int(extra.get("limit") or 20)}
        elif tool == "cctv_news":
            base = {"date": as_of}
        return base

    # ------------------------------------------------------------------ #
    # envelope
    # ------------------------------------------------------------------ #

    def _envelope(self, domain: str, code: str, as_of: str, tool: str, raw: Any) -> Any | None:
        if not isinstance(raw, dict):
            return None
        data = raw.get("data")
        if data in (None, [], {}):
            return None
        meta = dict(raw.get("meta") or {})
        meta["data_source_status"] = _status(meta)
        meta["confidence"] = _CONFIDENCE.get(
            str(meta.get("confidence") or "").upper(), meta.get("confidence")
        )
        data_as_of = _data_as_of(domain, data, meta, as_of)
        detail = f"{tool} via {meta.get('source') or 'tickhub'}"
        if meta.get("cache_hit"):
            detail += " (cache)"
        if meta.get("degraded"):
            detail += f" (degraded: {meta['degraded']})"
        trace = meta.get("trace_id")
        if trace:
            detail += f" [trace={trace}]"
        return envelope_from_payload(
            domain=domain,
            code=code,
            payload=data,
            source=self.name,
            source_detail=detail,
            data_as_of=date_only(data_as_of) or as_of,
            meta=meta,
        )


def _status(meta: dict[str, Any]) -> str:
    if meta.get("stale") or meta.get("degraded") or meta.get("partial"):
        return "degraded"
    return "ok"


def _data_as_of(domain: str, data: Any, meta: dict[str, Any], fallback: str) -> str:
    declared = meta.get("data_as_of")
    if declared:
        return str(declared)
    if domain == "quote":
        # Realtime quote: its observation time is the fetch time, never the
        # requested analysis date. The router is expected to reject it for a
        # historical run.
        return str(meta.get("fetched_at") or fallback)
    rows = data
    if isinstance(data, dict):
        for key in ("items", "rows", "list", "quotes", "records", "news"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
        else:
            rows = [data]
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        best = ""
        for item in rows[:50]:
            for key in _DATE_KEYS:
                value = item.get(key)
                if value:
                    best = max(best, date_only(value))
                    break
        if best:
            return best
    return fallback


def _shift(value: str, days: int) -> str:
    try:
        return (date.fromisoformat(value) + timedelta(days=days)).isoformat()
    except ValueError:
        return value


def sync(since: str | None = None) -> dict[str, Any]:
    """Pull the market-wide 7x24 news feed and report a compact summary."""
    since = since or (date.today() - timedelta(days=7)).isoformat()
    provider = TickHubProvider("tickhub_cn", {"package": "tickhub-cn"})
    raw = provider._client().call("eastmoney_global_news", {"limit": 200})
    data = raw.get("data") if isinstance(raw, dict) else raw
    rows = [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
    if rows and any(key in rows[0] for key in _DATE_KEYS):
        rows = [
            row
            for row in rows
            if max((date_only(row.get(key) or "") for key in _DATE_KEYS), default="") >= since
        ]
    return {
        "ok": True,
        "since": since,
        "count": len(rows),
        "source": "tickhub_cn/eastmoney_global_news",
    }


__all__ = ["DOMAIN_TOOLS", "TickHubProvider", "sync"]
