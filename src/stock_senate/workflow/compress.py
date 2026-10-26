"""Deterministic context compression for the remote prompt path.

The offline provider generates from the structured context and never builds a
prompt, so this module only ever affects remote providers. Context above
``workflow.compression.trigger_tokens`` is split on evidence boundaries, each
chunk is reduced until it fits ``chunk_tokens``, and citation/source fields
survive verbatim so every surviving claim can still be traced back.

The reduction is structural, never generative: it drops long prose and surplus
list items instead of asking a model to summarise, which keeps the same input
producing the same prompt and leaves numeric facts untouched.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

CITATION_KEYS = frozenset(
    {
        "citation",
        "citations",
        "source",
        "source_detail",
        "url",
        "urls",
        "link",
        "links",
        "data_as_of",
        "as_of",
    }
)
_CITATION_LISTS = frozenset({"citation", "citations"})

DEFAULT_TRIGGER_TOKENS = 24000
DEFAULT_CHUNK_TOKENS = 6000


def estimate_tokens(text: str) -> int:
    """Conservative token estimate: one per CJK character, four per latin run."""
    cjk = 0
    other = 0
    for char in text:
        if "\u4e00" <= char <= "\u9fff" or "\u3000" <= char <= "\u303f":
            cjk += 1
        else:
            other += 1
    return cjk + (other + 3) // 4


def settings_from_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(((config or {}).get("workflow") or {}).get("compression") or {})


@dataclass(frozen=True)
class CompressionRecord:
    enabled: bool
    triggered: bool
    original_tokens: int
    compressed_tokens: int
    chunks: int
    citations: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "triggered": self.triggered,
            "original_tokens": self.original_tokens,
            "compressed_tokens": self.compressed_tokens,
            "chunks": self.chunks,
            "citations": self.citations,
        }


def _serialize(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _tokens(value: Any) -> int:
    return estimate_tokens(_serialize(value))


def _units(context: Mapping[str, Any]) -> Iterator[tuple[str, Any]]:
    for key, value in context.items():
        name = str(key)
        if isinstance(value, list):
            if not value:
                yield name, []
            for item in value:
                yield name, [item]
        else:
            yield name, value


def _append_unit(chunk: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    merged = dict(chunk)
    existing = merged.get(key)
    if isinstance(existing, list) and isinstance(value, list):
        merged[key] = existing + value
    else:
        merged[key] = value
    return merged


def _pack(context: Mapping[str, Any], chunk_tokens: int) -> list[dict[str, Any]]:
    """Group whole evidence units into chunks no larger than ``chunk_tokens``."""
    chunks: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for key, value in _units(context):
        candidate = _append_unit(current, key, value)
        if current and _tokens(candidate) > chunk_tokens:
            chunks.append(current)
            current = _append_unit({}, key, value)
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _shrink(value: Any, *, max_string: int, max_items: int, keep_citations: bool) -> Any:
    if isinstance(value, str):
        if len(value) <= max_string:
            return value
        return value[:max_string] + "…"
    if isinstance(value, list):
        return [
            _shrink(
                item,
                max_string=max_string,
                max_items=max_items,
                keep_citations=keep_citations,
            )
            for item in value[:max_items]
        ]
    if isinstance(value, dict):
        reduced: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if keep_citations and name in CITATION_KEYS:
                # Citations are the audit trail: keep them whole.
                reduced[name] = item
            elif not keep_citations and name in _CITATION_LISTS:
                continue
            elif isinstance(item, (str, list, dict)) and not item:
                continue
            else:
                reduced[name] = _shrink(
                    item,
                    max_string=max_string,
                    max_items=max_items,
                    keep_citations=keep_citations,
                )
        return reduced
    return value


def _fit(chunk: dict[str, Any], budget: int, *, keep_citations: bool) -> dict[str, Any]:
    for max_string, max_items in (
        (max(200, budget), 40),
        (max(160, budget // 2), 24),
        (max(120, budget // 4), 12),
        (max(80, budget // 8), 6),
        (max(40, budget // 16), 3),
    ):
        candidate = _shrink(
            chunk,
            max_string=max_string,
            max_items=max_items,
            keep_citations=keep_citations,
        )
        if _tokens(candidate) <= budget:
            return candidate
    return _shrink(chunk, max_string=32, max_items=1, keep_citations=keep_citations)


def _count_citations(value: Any) -> int:
    if isinstance(value, dict):
        total = 0
        for key, item in value.items():
            if str(key) in {"citation", "citations"} and isinstance(item, list):
                total += len(item)
            else:
                total += _count_citations(item)
        return total
    if isinstance(value, list):
        return sum(_count_citations(item) for item in value)
    if isinstance(value, str):
        return value.count("https://") + value.count("http://")
    return 0


def compress_context(
    context: Mapping[str, Any], settings: Mapping[str, Any] | None = None
) -> tuple[dict[str, Any], CompressionRecord]:
    """Return ``(payload, record)``; ``payload`` is safe to serialise into a prompt."""
    section = dict(settings or {})
    enabled = section.get("enabled", True) is not False
    trigger = int(section.get("trigger_tokens") or DEFAULT_TRIGGER_TOKENS)
    chunk_tokens = int(section.get("chunk_tokens") or DEFAULT_CHUNK_TOKENS)
    keep_citations = section.get("keep_citations", True) is not False
    original_tokens = _tokens(context)

    if not enabled or original_tokens <= trigger:
        return dict(context), CompressionRecord(
            enabled=enabled,
            triggered=False,
            original_tokens=original_tokens,
            compressed_tokens=original_tokens,
            chunks=1,
            citations=_count_citations(context),
        )

    chunks = _pack(context, chunk_tokens)
    reduced: dict[str, Any] = {}
    for chunk in chunks:
        fitted = _fit(chunk, chunk_tokens, keep_citations=keep_citations)
        for key, value in fitted.items():
            reduced = _append_unit(reduced, key, value)

    return reduced, CompressionRecord(
        enabled=True,
        triggered=True,
        original_tokens=original_tokens,
        compressed_tokens=_tokens(reduced),
        chunks=len(chunks),
        citations=_count_citations(reduced),
    )


__all__ = [
    "CITATION_KEYS",
    "CompressionRecord",
    "compress_context",
    "estimate_tokens",
    "settings_from_config",
]
