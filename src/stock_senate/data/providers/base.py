"""Provider contract.

A provider turns one data domain for one code/as-of pair into a
``DataEnvelope``. It never invents values: when the source has nothing it
raises ``ProviderNotAvailable`` and lets the router decide what to do.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from stock_senate.data.envelope import DataEnvelope


class ProviderNotAvailable(RuntimeError):
    """The provider is unreachable or holds no record for this request."""


class ProviderError(RuntimeError):
    """The provider answered, but the answer is unusable (bad payload, etc.)."""


# Tool names used by ``agent_roles.toml`` are finer than data domains. Keeping
# the mapping here means a role never needs to know provider details.
TOOL_TO_DOMAIN: dict[str, str] = {
    "kline": "kline",
    "quote": "quote",
    "money_flow": "money_flow",
    "financial": "financial",
    "valuation": "financial",
    "announcement": "announcement",
    "research": "research",
    "news": "news",
    "news_search": "news",
    "social_buzz": "social_buzz",
    "policy_feed": "policy_feed",
    "dragon_tiger": "dragon_tiger",
    "lockup_calendar": "lockup_calendar",
    "risk_event": "risk_event",
}


def tool_to_domain(tool: str) -> str:
    return TOOL_TO_DOMAIN.get(tool, tool)


class Provider(ABC):
    """Base class for every data provider."""

    kind = "base"

    def __init__(self, name: str, config: dict[str, Any] | None = None, *,
                 home: Path | None = None) -> None:
        self.name = name
        self.config = dict(config or {})
        self.home = Path(home).expanduser() if home else None

    @property
    def domains(self) -> set[str]:
        return set(self.config.get("domains") or [])

    def supports(self, domain: str) -> bool:
        declared = self.domains
        return not declared or domain in declared

    @abstractmethod
    def fetch(self, domain: str, code: str, as_of: str,
              params: dict[str, Any] | None = None) -> DataEnvelope:
        """Return one envelope or raise ``ProviderNotAvailable`` / ``ProviderError``."""

    def _root(self) -> Path:
        raw = self.config.get("root")
        if not raw:
            return (self.home or Path.cwd()) / "data"
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (self.home or Path.cwd()) / path
        return path


def envelope_from_payload(
    *,
    domain: str,
    code: str,
    payload: Any,
    source: str,
    source_detail: str,
    data_as_of: str,
    meta: dict[str, Any] | None = None,
    default_confidence: str = "medium",
) -> DataEnvelope:
    """Build an envelope from a provider response, honouring provider metadata."""
    from stock_senate.data.envelope import (
        Confidence,
        Freshness,
        SourceStatus,
        classify_freshness,
        now_iso,
    )

    meta = dict(meta or {})
    fetched_at = str(meta.get("fetched_at") or now_iso())
    confidence_raw = str(meta.get("confidence") or default_confidence).lower()
    confidence = Confidence(confidence_raw) if confidence_raw in {
        item.value for item in Confidence
    } else Confidence.MEDIUM
    status_raw = str(meta.get("data_source_status") or meta.get("status") or "ok").lower()
    status = SourceStatus(status_raw) if status_raw in {
        item.value for item in SourceStatus
    } else SourceStatus.OK
    freshness_raw = str(meta.get("freshness") or "").lower()
    freshness = Freshness(freshness_raw) if freshness_raw in {
        item.value for item in Freshness
    } else classify_freshness(data_as_of)
    return DataEnvelope(
        domain=domain,
        code=code,
        data_as_of=data_as_of,
        payload=payload,
        source=str(meta.get("source") or source),
        source_detail=str(meta.get("source_detail") or source_detail),
        fetched_at=fetched_at,
        freshness=freshness,
        confidence=confidence,
        data_source_status=status,
    )


__all__ = [
    "Provider",
    "ProviderError",
    "ProviderNotAvailable",
    "TOOL_TO_DOMAIN",
    "envelope_from_payload",
    "tool_to_domain",
]
