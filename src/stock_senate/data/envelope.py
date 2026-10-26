"""The single data contract every provider must satisfy.

``DataEnvelope`` is the only shape the rest of the system consumes. A provider
never returns raw JSON: it fills an envelope, and the router is the only code
allowed to decide that a domain is ``missing`` because every source failed.
Providers may forward a factual ``degraded`` flag reported by their upstream
(stale cache, partial batch); they never invent data to fill a gap.

``forbid_llm_fill`` is enforced structurally: payloads carrying keys that mark
model-invented values are rejected before an envelope is built, so a number
that did not come from a provider cannot enter the graph.
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

FORBIDDEN_FILL_KEYS = frozenset(
    {"llm_fill", "llm_inferred", "synthetic", "model_estimate", "fabricated", "invented"}
)


class Freshness(str, Enum):
    REALTIME = "realtime"
    CACHED = "cached"
    ARCHIVE = "archive"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SourceStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    MISSING = "missing"


class DataEnvelope(BaseModel):
    """Ten addressed fields, closed to extras."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: str
    code: str
    data_as_of: str
    payload: Any = None
    source: str
    source_detail: str = ""
    fetched_at: str
    freshness: Freshness
    confidence: Confidence
    data_source_status: SourceStatus

    @field_validator("data_as_of", "fetched_at")
    @classmethod
    def _iso_timestamp(cls, value: str) -> str:
        if not value:
            return value
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"timestamp must be ISO-8601: {value!r}") from exc
        return value

    @property
    def usable(self) -> bool:
        return self.data_source_status is not SourceStatus.MISSING

    @property
    def as_of_date(self) -> str:
        return (self.data_as_of or "")[:10]

    def degraded(self, reason: str) -> DataEnvelope:
        detail = f"{self.source_detail} | {reason}" if self.source_detail else reason
        return self.model_copy(
            update={
                "data_source_status": SourceStatus.DEGRADED,
                "source_detail": detail,
                "confidence": Confidence.LOW,
            }
        )

    def appendix_row(self) -> dict[str, str]:
        return {
            "domain": self.domain,
            "source": self.source,
            "data_as_of": self.data_as_of,
            "freshness": self.freshness.value,
            "confidence": self.confidence.value,
            "data_source_status": self.data_source_status.value,
        }

    def summary(self) -> str:
        return (
            f"{self.domain}: {self.data_source_status.value} via {self.source} "
            f"(as_of={self.data_as_of or 'n/a'}, freshness={self.freshness.value}, "
            f"confidence={self.confidence.value})"
        )

    @classmethod
    def missing(
        cls,
        domain: str,
        code: str,
        *,
        as_of: str,
        detail: str,
        source: str = "router",
    ) -> DataEnvelope:
        return cls(
            domain=domain,
            code=code,
            data_as_of=as_of,
            payload=None,
            source=source,
            source_detail=detail,
            fetched_at=now_iso(),
            freshness=Freshness.ARCHIVE,
            confidence=Confidence.LOW,
            data_source_status=SourceStatus.MISSING,
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def assert_no_llm_fill(payload: Any, *, path: str = "payload") -> None:
    """Reject payloads that declare model-generated values."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in FORBIDDEN_FILL_KEYS:
                raise ValueError(f"forbid_llm_fill: {path}.{key} marks a model-filled value")
            assert_no_llm_fill(value, path=f"{path}.{key}")
    elif isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            assert_no_llm_fill(item, path=f"{path}[{index}]")


def classify_freshness(data_as_of: str, *, realtime_max_seconds: int = 900,
                       cached_max_seconds: int = 86400) -> Freshness:
    """Classify an envelope's freshness against wall-clock now."""
    try:
        moment = datetime.fromisoformat(data_as_of.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return Freshness.ARCHIVE
    age = (datetime.now(timezone.utc) - moment).total_seconds()
    if age <= realtime_max_seconds:
        return Freshness.REALTIME
    if age <= cached_max_seconds:
        return Freshness.CACHED
    return Freshness.ARCHIVE


def date_only(value: Any) -> str:
    """Best-effort ISO date extraction from provider timestamps."""
    if isinstance(value, date_cls):
        return value.isoformat()
    if not value:
        return ""
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text[:10]


__all__ = [
    "Confidence",
    "DataEnvelope",
    "FORBIDDEN_FILL_KEYS",
    "Freshness",
    "SourceStatus",
    "assert_no_llm_fill",
    "classify_freshness",
    "date_only",
    "now_iso",
]
