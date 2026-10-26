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
from stock_senate.data.sanitize import sanitize_external_text


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

    def __init__(
        self, name: str, config: dict[str, Any] | None = None, *, home: Path | None = None
    ) -> None:
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
    def fetch(
        self, domain: str, code: str, as_of: str, params: dict[str, Any] | None = None
    ) -> DataEnvelope:
        """Return one envelope or raise ``ProviderNotAvailable`` / ``ProviderError``."""

    def _root(self) -> Path:
        raw = self.config.get("root")
        if not raw:
            return (self.home or Path.cwd()) / "data"
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (self.home or Path.cwd()) / path
        return path

    def _safe_subdir(self, domain: str, code: str) -> tuple[Path, str]:
        """Resolve ``root/domain/code`` and refuse anything outside ``root``.

        Returns the directory plus the canonical code. Every path-building
        provider goes through this method, so a request that reaches a provider
        directly still cannot read outside the archive.
        """
        from stock_senate.data.codes import normalize_code

        try:
            canonical = normalize_code(code)
        except ValueError as exc:
            raise ProviderNotAvailable(f"{domain}: 非法标的代码，已拒绝") from exc
        root = self._root().resolve()
        candidate = (root / domain / canonical).resolve()
        if not candidate.is_relative_to(root):
            raise ProviderNotAvailable(f"{domain}: code escapes archive root")
        return candidate, canonical


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
    confidence = (
        Confidence(confidence_raw)
        if confidence_raw in {item.value for item in Confidence}
        else Confidence.MEDIUM
    )
    status_raw = str(meta.get("data_source_status") or meta.get("status") or "ok").lower()
    status = (
        SourceStatus(status_raw)
        if status_raw in {item.value for item in SourceStatus}
        else SourceStatus.OK
    )
    freshness_raw = str(meta.get("freshness") or "").lower()
    freshness = (
        Freshness(freshness_raw)
        if freshness_raw in {item.value for item in Freshness}
        else classify_freshness(data_as_of)
    )
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


def _sanitize_payload(value: Any, counters: dict[str, Any]) -> Any:
    if isinstance(value, str):
        result = sanitize_external_text(value)
        counters["injections"] += result.removed_injections
        counters["sentences"] += result.removed_sentences
        counters["flags"].update(result.flags)
        return result.text
    if isinstance(value, dict):
        return {key: _sanitize_payload(item, counters) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_payload(item, counters) for item in value]
    return value


def sanitize_envelope(envelope: DataEnvelope) -> DataEnvelope:
    """Strip injected instructions from an envelope's external text.

    The cleaning is recorded in ``source_detail`` so a reader can tell that the
    upstream prose was rewritten; nothing is dropped silently.
    """
    counters: dict[str, Any] = {"injections": 0, "sentences": 0, "flags": set()}
    payload = _sanitize_payload(envelope.payload, counters)
    if not counters["injections"] and not counters["sentences"]:
        return envelope
    note = f"sanitized: injections={counters['injections']}, sentences={counters['sentences']}"
    if counters["flags"]:
        # Record how many prohibited terms were found, not the terms themselves:
        # the note travels in the digest and the prompt, which must stay clean.
        note += f", matched_terms={len(set(counters['flags']))}"
    detail = f"{envelope.source_detail} | {note}" if envelope.source_detail else note
    return envelope.model_copy(update={"payload": payload, "source_detail": detail})


class SanitizingProvider(Provider):
    """Single exit that cleans external text before it leaves the data layer.

    Wrapping every provider at the factory means no provider — current or
    future — can hand raw upstream prose to the digest or the prompt.
    """

    def __init__(self, child: Provider) -> None:
        super().__init__(child.name, child.config, home=child.home)
        self.child = child
        self.kind = child.kind

    @property
    def domains(self) -> set[str]:
        return self.child.domains

    @property
    def children(self) -> list[Provider]:
        return list(getattr(self.child, "children", []))

    def fetch(
        self, domain: str, code: str, as_of: str, params: dict[str, Any] | None = None
    ) -> DataEnvelope:
        return sanitize_envelope(self.child.fetch(domain, code, as_of, params))


__all__ = [
    "Provider",
    "ProviderError",
    "ProviderNotAvailable",
    "SanitizingProvider",
    "TOOL_TO_DOMAIN",
    "envelope_from_payload",
    "sanitize_envelope",
    "tool_to_domain",
]
