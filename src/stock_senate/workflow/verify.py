"""Cross-check role output against the factual digest.

A model may only quote numbers that exist in the data payload it was given.
This module compares every numeric ``Fact`` in an analyst report with the
digest of the same domain and flags values that cannot be traced back. Flagged
facts are removed rather than tolerated: an untraceable number is exactly the
kind of fabricated data the product forbids, so the report keeps the
conclusion but loses the evidence it cannot support.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from stock_senate.roles.schemas import AnalystReport, Citation, Fact

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_TOLERANCE = 1e-6


def _numbers(text: Any) -> list[float]:
    found: list[float] = []
    for token in _NUMBER.findall(str(text)):
        try:
            found.append(float(token))
        except ValueError:  # pragma: no cover - regex guarantees a float
            continue
    return found


def _matches(value: float, candidates: list[float]) -> bool:
    for candidate in candidates:
        scale = max(1.0, abs(value), abs(candidate))
        if abs(value - candidate) <= _TOLERANCE * scale:
            return True
    return False


def _scalar_numbers(value: Any, out: list[float]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        out.append(float(value))
        return
    if isinstance(value, Mapping):
        for item in value.values():
            _scalar_numbers(item, out)
        return
    if isinstance(value, (list, tuple)):
        # A list heading is a count; titles are not numbers and are excluded.
        out.append(float(len(value)))


def domain_numbers(entry: Mapping[str, Any] | None) -> list[float]:
    """Every number the digest can vouch for in one domain."""
    if not entry:
        return []
    numbers: list[float] = []
    for fact in entry.get("facts") or []:
        numbers.extend(_numbers((fact or {}).get("value", "")))
    _scalar_numbers(entry.get("metrics") or {}, numbers)
    return numbers


def cross_check(payload: Mapping[str, Any], digest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return conflicts; an empty list means everything is traceable."""
    domains = dict((digest or {}).get("domains") or {})
    conflicts: list[dict[str, Any]] = []

    for fact in payload.get("facts") or []:
        fact = fact or {}
        domain = str(fact.get("domain") or "")
        entry = domains.get(domain)
        if entry is None or entry.get("status") == "missing":
            if _numbers(fact.get("value", "")):
                conflicts.append(
                    {
                        "kind": "uncited_value",
                        "domain": domain,
                        "label": str(fact.get("label") or ""),
                        "value": str(fact.get("value") or ""),
                        "reason": "数据域缺失，事实无法核验",
                    }
                )
            continue
        expected = domain_numbers(entry)
        for number in _numbers(fact.get("value", "")):
            if not _matches(number, expected):
                conflicts.append(
                    {
                        "kind": "uncited_value",
                        "domain": domain,
                        "label": str(fact.get("label") or ""),
                        "value": str(fact.get("value") or ""),
                        "reason": f"数值 {number:g} 不在 {domain} 数据载荷中",
                    }
                )
                break

    for citation in payload.get("citations") or []:
        citation = citation or {}
        domain = str(citation.get("domain") or "")
        entry = domains.get(domain)
        if entry is None or entry.get("status") == "missing":
            conflicts.append(
                {
                    "kind": "citation_mismatch",
                    "domain": domain,
                    "source": str(citation.get("source") or ""),
                    "reason": "引用的数据域不存在或缺失",
                }
            )
            continue
        claimed = str(citation.get("source") or "")
        actual = str(entry.get("source") or "")
        if claimed and actual and claimed != actual:
            conflicts.append(
                {
                    "kind": "citation_mismatch",
                    "domain": domain,
                    "source": claimed,
                    "reason": f"引用来源 {claimed!r} 与实际来源 {actual!r} 不一致",
                }
            )

    return conflicts


def apply_conflicts(
    report: AnalystReport,
    conflicts: list[dict[str, Any]],
    ctx: Any = None,
) -> AnalystReport:
    """Strip unverifiable facts/citations and record why."""
    if not conflicts:
        return report

    bad_values = {
        (item.get("label"), item.get("value"))
        for item in conflicts
        if item.get("kind") == "uncited_value"
    }
    facts: list[Fact] = [
        fact for fact in report.facts if (fact.label, fact.value) not in bad_values
    ]
    bad_domains = {
        str(item.get("domain")) for item in conflicts if item.get("kind") == "citation_mismatch"
    }
    citations: list[Citation] = [
        citation for citation in report.citations if citation.domain not in bad_domains
    ]

    dropped = len(report.facts) - len(facts)
    note = "数据核验："
    if dropped:
        note += f"剔除 {dropped} 条无法在数据载荷中回溯的事实；"
    if len(report.citations) - len(citations):
        note += f"剔除 {len(report.citations) - len(citations)} 条来源不一致的引用；"
    note += "结论仅覆盖可核验部分。"

    gaps = list(report.data_gaps)
    if dropped:
        gaps.append("部分事实无法回溯到数据载荷，已从报告移除。")
    points = list(report.key_points)
    points.append(note)

    if ctx is not None and dropped:
        ctx.skipped.append(
            {"role": report.role, "reason": f"{dropped} 条事实未通过数据核验，已剔除"}
        )

    return report.model_copy(
        update={
            "facts": facts,
            "citations": citations,
            "data_gaps": gaps,
            "key_points": points,
            "notes": (report.notes + " " + note).strip(),
            "partial": True,
        }
    )


__all__ = ["apply_conflicts", "cross_check", "domain_numbers"]
