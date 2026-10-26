"""Text safety: prompt-injection stripping and the no-executable-price rule.

External announcements, forum posts and research PDFs are the largest untrusted
input surface. Text is stripped of instruction-like payloads before it reaches
a prompt, and any sentence that would smuggle an executable price level or an
order instruction is dropped entirely. The second rule is structural: the
report cannot contain a level even if an upstream document does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_INJECTION_PATTERNS = (
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?",
    r"disregard\s+(all\s+)?(previous|prior|above)",
    r"system\s*prompt",
    r"you\s+are\s+now\s+(a|an|the)\s+",
    r"忽略(以上|之前|前面|上述)?(所有)?(的)?(指令|要求|规则|提示)",
    r"无视(以上|之前|前面|上述)?(所有)?(的)?(指令|要求|规则|提示)",
    r"你现在(是|扮演)",
    r"系统提示词",
    r"开发者模式",
    r"jailbreak",
)

PROHIBITED_TERMS = (
    "建仓价",
    "建仓点位",
    "止损位",
    "止损价",
    "目标价",
    "目标价位",
    "仓位比例",
    "仓位建议",
    "满仓",
    "买入价",
    "卖出价",
    "挂单价",
    "下单",
    "委托单",
    "券商账户",
    "order_id",
    "broker_account",
    "auto_order",
)

_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in _INJECTION_PATTERNS), re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")


@dataclass
class SanitizeResult:
    text: str
    removed_injections: int = 0
    removed_sentences: int = 0
    flags: list[str] = field(default_factory=list)


def sanitize_external_text(text: str) -> SanitizeResult:
    """Strip injection phrases and price/order sentences from untrusted text."""
    if not text:
        return SanitizeResult(text="")
    result = SanitizeResult(text=str(text))
    result.removed_injections = len(_INJECTION_RE.findall(result.text))
    if result.removed_injections:
        result.text = _INJECTION_RE.sub("[外来指令已移除]", result.text)
    kept: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(result.text):
        hits = compliance_scan(sentence)
        if hits:
            result.removed_sentences += 1
            result.flags.extend(hits)
            continue
        kept.append(sentence)
    result.text = "".join(kept).strip()
    return result


def compliance_scan(text: str) -> list[str]:
    """Return the prohibited terms found in ``text`` (case-insensitive)."""
    if not text:
        return []
    lowered = str(text).lower()
    return [term for term in PROHIBITED_TERMS if term.lower() in lowered]


def compliance_filter(text: str, replacement: str = "［已按要求移除可执行价位与指令］") -> str:
    """Sentence-level filter used on generated report fragments."""
    if not text:
        return ""
    kept = [
        sentence
        for sentence in _SENTENCE_SPLIT.split(str(text))
        if not compliance_scan(sentence)
    ]
    stripped = "".join(kept).strip()
    return stripped if stripped else replacement


def assert_compliant(text: str, *, where: str = "report") -> None:
    hits = compliance_scan(text)
    if hits:
        raise ValueError(f"{where} 含可执行价位/交易指令用语：{hits}")


__all__ = [
    "PROHIBITED_TERMS",
    "SanitizeResult",
    "assert_compliant",
    "compliance_filter",
    "compliance_scan",
    "sanitize_external_text",
]
