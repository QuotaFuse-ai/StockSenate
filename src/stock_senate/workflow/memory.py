"""Long-term reflection memory.

Short-term state lives in ``RunContext`` and dies with the run. This module is
the part that survives it: every finished run appends one reflection to a JSONL
file under the state root, and the next analysis of the same code receives the
most recent reflections in its analyst and manager contexts.

Storage is deliberately plain. One JSON object per line, append-only, newest
last, so the file can be read with ``tail`` and a corrupt line can never take
down a run. Text is passed through the compliance filter first: memory keeps
research conclusions, never executable price levels or order instructions.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stock_senate.data.sanitize import compliance_filter, compliance_scan
from stock_senate.observability.logging import get_logger

DEFAULT_MAX_ENTRIES = 6
DEFAULT_PATH = "memory/reflections.jsonl"
_MAX_TEXT = 240
_MAX_ITEMS = 3
_MAX_SCAN_LINES = 2000
_MAX_ENTRIES_CAP = 50


def memory_settings(config: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(((config or {}).get("workflow") or {}).get("memory") or {})


def _clean(text: Any, limit: int = _MAX_TEXT) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    if compliance_scan(value):
        # Memory records conclusions, so a sentence that was entirely a price
        # level or an order instruction is dropped rather than replaced by a
        # placeholder that would read back as content next run.
        value = compliance_filter(value, replacement="").strip()
        if not value:
            return ""
    return value[:limit]


def _clean_items(values: Any, *, limit: int = _MAX_ITEMS) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    cleaned: list[str] = []
    for item in values[:limit]:
        text = _clean(item)
        if text:
            cleaned.append(text)
    return cleaned


def _number(value: Any) -> float | None:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def build_reflection(
    *,
    code: str,
    date: str,
    run_id: str,
    decision: Mapping[str, Any],
    quality: Mapping[str, Any],
    research: Mapping[str, Any],
    trade: Mapping[str, Any],
    hitl: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Turn a finished run into the compact record memory keeps."""
    rating = _clean(decision.get("rating"), 32) or "unknown"
    partial = bool(decision.get("partial"))
    low_quality = bool(decision.get("low_quality") or quality.get("low_quality"))
    lessons: list[str] = []
    if rating == "unknown":
        lessons.append("终审未给出评级，下一次应先补齐关键数据域。")
    if low_quality:
        lessons.append("质量门判定偏低，下一次优先修复缺失证据与引用。")
    if partial:
        lessons.append("本次存在跳过角色或预算熔断，结论只可作参考。")
    if not list(decision.get("risks") or []):
        lessons.append("风险清单为空，下一次至少给出可核查的风险。")
    return {
        "code": _clean(code, 16),
        "date": _clean(date, 10),
        "run_id": _clean(run_id, 64),
        "rating": rating,
        "stance": _clean(research.get("stance"), 32) or "unknown",
        "direction": _clean(trade.get("direction"), 32) or "unknown",
        "confidence": _number(decision.get("confidence")),
        "quality_score": _number(quality.get("score")),
        "low_quality": low_quality,
        "partial": partial,
        "reasons": _clean_items(decision.get("reasons")),
        "risks": _clean_items(decision.get("risks")),
        "lessons": lessons[:_MAX_ITEMS],
        "hitl": _clean((hitl or {}).get("decision"), 32),
    }


def injectable(entries: Sequence[Mapping[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Project stored reflections onto the small shape prompts receive.

    The file is plain JSONL that a user can edit, so every field is re-scanned
    on read: a hand-written price level must not become prompt content.
    """
    out: list[dict[str, Any]] = []
    for entry in entries[-max(0, limit) :]:
        stored = entry.get("lessons")
        raw_lessons = stored[:2] if isinstance(stored, (list, tuple)) else []
        lessons = [
            text for item in raw_lessons if isinstance(item, str) and (text := _clean(item, 160))
        ]
        out.append(
            {
                "date": _clean(entry.get("date"), 10),
                "rating": _clean(entry.get("rating"), 32),
                "stance": _clean(entry.get("stance"), 32) or "unknown",
                "quality_score": entry.get("quality_score"),
                "low_quality": entry.get("low_quality"),
                "lessons": lessons,
            }
        )
    return out


@dataclass(frozen=True)
class MemoryStore:
    home: Path
    settings: Mapping[str, Any] | None = None

    @property
    def enabled(self) -> bool:
        return (self.settings or {}).get("enabled", True) is not False

    @property
    def path(self) -> Path:
        configured = str((self.settings or {}).get("path") or DEFAULT_PATH).strip()
        configured = configured.replace("\\", "/")
        candidate = Path(configured)
        rooted = configured.startswith(("/", "~")) or (len(configured) > 1 and configured[1] == ":")
        if rooted or candidate.is_absolute() or ".." in candidate.parts:
            get_logger("memory").warning(
                "memory path must stay under the state root; using the default",
                extra={"event": "memory_path_rejected"},
            )
            return Path(self.home) / DEFAULT_PATH
        return Path(self.home) / candidate

    @property
    def max_entries(self) -> int:
        try:
            value = int((self.settings or {}).get("max_entries", DEFAULT_MAX_ENTRIES))
        except (TypeError, ValueError):
            return DEFAULT_MAX_ENTRIES
        return min(max(0, value), _MAX_ENTRIES_CAP)

    def recent(self, *, code: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        path = self.path
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError as exc:
            get_logger("memory").warning(
                "memory read failed", extra={"event": "memory_read_failed", "error": str(exc)}
            )
            return []

        if limit is None:
            wanted = self.max_entries
        else:
            wanted = min(max(0, limit), _MAX_ENTRIES_CAP)
        entries: list[dict[str, Any]] = []
        for line in lines[-_MAX_SCAN_LINES:]:
            text = line.strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            if code and str(parsed.get("code") or "") != code:
                continue
            entries.append(parsed)
        return entries[-wanted:] if wanted else []

    def remember(self, record: Mapping[str, Any]) -> bool:
        """Append one reflection. Never raises: memory must not fail a run."""
        if not self.enabled:
            return False
        path = self.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(dict(record), ensure_ascii=False, default=str)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            get_logger("memory").warning(
                "memory write failed", extra={"event": "memory_write_failed", "error": str(exc)}
            )
            return False
        return True


__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_PATH",
    "MemoryStore",
    "build_reflection",
    "injectable",
    "memory_settings",
]
