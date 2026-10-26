"""Data providers and the factory the router uses to build them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from stock_senate.data.providers.base import (
    Provider,
    ProviderError,
    ProviderNotAvailable,
    TOOL_TO_DOMAIN,
    envelope_from_payload,
    tool_to_domain,
)
from stock_senate.data.providers.composite import CompositeProvider
from stock_senate.data.providers.local_json import LocalJsonProvider
from stock_senate.data.providers.newsieve import NewsieveProvider
from stock_senate.data.providers.sql import SqlProvider
from stock_senate.data.providers.text_doc import TextDocProvider
from stock_senate.data.providers.tickhub import TickHubProvider

# ``kind`` in data_source.toml -> provider class. Names are resolved first so
# tickhub_cn/tickhub_us/newsieve win over the generic MCP kind.
_BY_KIND = {
    "json_file": LocalJsonProvider,
    "text_doc": TextDocProvider,
    "sql": SqlProvider,
    "composite": CompositeProvider,
    "mcp": TickHubProvider,
}


def build_provider(name: str, spec: dict[str, Any] | None = None, *,
                   home: Path | None = None) -> Provider:
    spec = dict(spec or {})
    lowered = name.lower()
    if lowered.startswith("newsieve"):
        cls: type[Provider] = NewsieveProvider
    elif lowered.startswith("tickhub"):
        cls = TickHubProvider
    else:
        kind = str(spec.get("kind") or "mcp")
        cls = _BY_KIND.get(kind)
        if cls is None:
            raise ValueError(f"未知 provider 类型 {kind!r}（{name}）")
    return cls(name, spec, home=home)


def build_providers(section: dict[str, Any] | None, *,
                    home: Path | None = None) -> dict[str, Provider]:
    built: dict[str, Provider] = {}
    for name, spec in (section or {}).items():
        if not isinstance(spec, dict) or spec.get("enabled") is False:
            continue
        try:
            built[name] = build_provider(name, spec, home=home)
        except ValueError:
            continue
    return built


# Providers that reach the network. Everything else reads local archives, so
# ``mode = "local"`` never touches a socket.
REMOTE_KINDS = {"mcp"}


def is_remote(provider: Provider) -> bool:
    if provider.kind == "composite":
        return any(is_remote(child) for child in provider.children)  # type: ignore[attr-defined]
    return provider.kind in REMOTE_KINDS


__all__ = [
    "CompositeProvider",
    "LocalJsonProvider",
    "Provider",
    "ProviderError",
    "ProviderNotAvailable",
    "REMOTE_KINDS",
    "SqlProvider",
    "TextDocProvider",
    "TickHubProvider",
    "TOOL_TO_DOMAIN",
    "build_provider",
    "build_providers",
    "envelope_from_payload",
    "is_remote",
    "tool_to_domain",
]
