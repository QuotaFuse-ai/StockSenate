"""Composite provider.

Tries its children in declared order and returns the first usable envelope.
Used when one domain is served by a fixed chain that differs from the global
routing table.
"""

from __future__ import annotations

from typing import Any

from stock_senate.data.envelope import DataEnvelope
from stock_senate.data.providers.base import Provider, ProviderNotAvailable


class CompositeProvider(Provider):
    kind = "composite"

    def __init__(self, name: str, config: dict[str, Any] | None = None, *,
                 home=None, children: list[Provider] | None = None) -> None:
        super().__init__(name, config, home=home)
        if children is not None:
            self.children = list(children)
        else:
            self.children = self._build_children()

    def _build_children(self) -> list[Provider]:
        from stock_senate.data.providers import build_provider

        specs = self.config.get("providers") or []
        built: list[Provider] = []
        for entry in specs:
            if isinstance(entry, str):
                built.append(build_provider(entry, {"kind": "mcp"}, home=self.home))
            elif isinstance(entry, dict):
                child = dict(entry)
                child_name = child.pop("name", None) or child.get("package") or "child"
                built.append(build_provider(str(child_name), child, home=self.home))
        return built

    def fetch(self, domain: str, code: str, as_of: str,
              params: dict[str, Any] | None = None) -> DataEnvelope:
        failures: list[str] = []
        for child in self.children:
            if not child.supports(domain):
                continue
            try:
                return child.fetch(domain, code, as_of, params)
            except ProviderNotAvailable as exc:
                failures.append(f"{child.name}: {exc}")
            except Exception as exc:  # noqa: BLE001 - provider boundary
                failures.append(f"{child.name}: {type(exc).__name__}: {exc}")
        raise ProviderNotAvailable(
            f"composite {self.name}: no child returned {domain}: "
            + "; ".join(failures or ["no child supports the domain"]))


__all__ = ["CompositeProvider"]
