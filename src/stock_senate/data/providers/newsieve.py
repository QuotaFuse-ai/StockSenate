"""Newsieve provider.

Newsieve owns news collection, deduplication and filtering; StockSenate only
consumes the result. The package is optional, so every entry point imports it
lazily and degrades honestly when it is absent.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from stock_senate.data.envelope import date_only
from stock_senate.data.providers.base import (
    Provider, ProviderNotAvailable, envelope_from_payload,
)

DOMAIN_TOOLS: dict[str, tuple[str, ...]] = {
    "news": ("news_search", "news"),
    "social_buzz": ("social_buzz",),
    "policy_feed": ("policy_feed",),
}

_MISSING = "newsieve 未安装；`pip install stocksenate[stockdata]` 后可用"


def _client(config: dict[str, Any] | None = None):
    try:
        import newsieve
    except ImportError as exc:
        raise ProviderNotAvailable(_MISSING) from exc
    factory = getattr(newsieve, "NewsieveClient", None) or getattr(newsieve, "Client", None)
    if factory is None:
        return newsieve
    config = config or {}
    try:
        return factory(base_url=config["base_url"]) if config.get("base_url") else factory()
    except TypeError:
        return factory()


class NewsieveProvider(Provider):
    kind = "mcp"

    def fetch(self, domain: str, code: str, as_of: str,
              params: dict[str, Any] | None = None):
        tools = DOMAIN_TOOLS.get(domain)
        if not tools:
            raise ProviderNotAvailable(f"newsieve: no tool mapped for domain {domain}")
        client = self._client()
        payload = self._call(client, tools, code, as_of, dict(params or {}))
        if payload in (None, [], {}):
            raise ProviderNotAvailable(f"newsieve: no {domain} items for {code} at {as_of}")
        return envelope_from_payload(
            domain=domain,
            code=code,
            payload=payload,
            source=self.name,
            source_detail=f"{self.config.get('package', 'newsieve')} via {'/'.join(tools)}",
            data_as_of=_data_as_of(payload, as_of),
            default_confidence="medium",
        )

    def _client(self):
        if not hasattr(self, "_client_instance"):
            self._client_instance = _client(self.config)
        return self._client_instance

    def _call(self, client, tools, code, as_of, params):
        lookback = int(params.pop("lookback_days", 0) or self.config.get("lookback_days", 30))
        since = params.pop("since", None) or _shift(as_of, -lookback)
        failures: list[str] = []
        for tool in tools:
            call = getattr(client, "call", None) or getattr(client, "fetch", None)
            if call is None:
                continue
            try:
                raw = call(tool, {"code": code, "since": since, "until": as_of, **params})
            except TypeError:
                try:
                    raw = call(tool, code=code, since=since, until=as_of, **params)
                except Exception as exc:  # noqa: BLE001 - provider boundary
                    failures.append(f"{tool}: {exc}")
                    continue
            except Exception as exc:  # noqa: BLE001 - provider boundary
                failures.append(f"{tool}: {exc}")
                continue
            return _unwrap(raw)
        if not failures:
            raise ProviderNotAvailable("newsieve: client exposes neither call() nor fetch()")
        raise ProviderNotAvailable("newsieve: " + "; ".join(failures))


def sync(since: str | None = None) -> dict[str, Any]:
    """Pull the latest news through Newsieve and report a compact summary."""
    since = since or (date.today() - timedelta(days=7)).isoformat()
    client = _client()
    trigger = getattr(client, "sync", None)
    if trigger is None:
        raise ProviderNotAvailable("newsieve: 客户端未提供 sync()，无法同步资讯")
    try:
        raw = trigger(since=since)
    except TypeError:
        raw = trigger(since)
    data = _unwrap(raw)
    items = data if isinstance(data, list) else (data or {}).get("items", [])
    return {
        "ok": True,
        "since": since,
        "count": len(items) if isinstance(items, list) else 0,
        "domains": sorted(DOMAIN_TOOLS),
    }


def _unwrap(raw: Any) -> Any:
    if isinstance(raw, dict) and "data" in raw and ("ok" in raw or "meta" in raw):
        return raw.get("data")
    return raw


def _data_as_of(payload: Any, fallback: str) -> str:
    rows = payload
    if isinstance(payload, dict):
        for key in ("items", "rows", "list", "records", "documents"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        else:
            rows = [payload]
    if isinstance(rows, list):
        best = ""
        for item in rows[:50]:
            if not isinstance(item, dict):
                continue
            for key in ("publish_time", "publish_date", "published_at", "date", "time"):
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


__all__ = ["DOMAIN_TOOLS", "NewsieveProvider", "sync"]
