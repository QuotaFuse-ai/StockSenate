"""Alert conditions and the facts they read.

The rule language is deliberately tiny — ``<dotted.path> <op> <literal>`` with
``or`` between clauses — and is parsed then compared in Python. Nothing is
handed to ``eval``, so a rule file can never execute code. Every fact comes from
a data envelope; a missing fact means "unknown", never a guessed value.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from stock_senate.data.envelope import DataEnvelope
from stock_senate.data.router import summarize
from stock_senate.workflow.digest import rows_of

_OPERATORS = (">=", "<=", "==", "!=", ">", "<")
_CONDITION = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.]*)\s*(>=|<=|==|!=|>|<)\s*(.+?)\s*$")
_OR = re.compile(r"\s+or\s+", re.IGNORECASE)
_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")

_HIGH_RISK = (
    "立案",
    "处罚",
    "退市",
    "违约",
    "冻结",
    "诉讼",
    "商誉减值",
    "预亏",
    "重大亏损",
    "资金占用",
    "违规担保",
    "评级下调",
    "被出具警示函",
)
_MID_RISK = ("风险提示", "问询", "监管", "下调", "亏损", "减持", "质押", "异常波动")

_SEVERITY_KEYS = ("severity", "risk_level", "risk_severity", "level")
_DATE_KEYS = ("date", "unlock_date", "expiry_date", "解禁日期")
_TITLE_KEYS = (
    "title",
    "headline",
    "summary",
    "subject",
    "content",
    "text",
    "name",
    "label",
    "type",
)
_RISK_LIST_KEYS = ("events", "items", "list", "records", "rows")
_LOCKUP_LIST_KEYS = ("upcoming", "items", "list", "records", "rows")


@dataclass(frozen=True)
class Condition:
    path: str
    op: str
    threshold: float | str | bool | None
    raw: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "op": self.op, "threshold": self.threshold}


@dataclass(frozen=True)
class ConditionGroup:
    """One or more clauses joined by ``or``; the rule fires when any fires."""

    clauses: tuple[Condition, ...]
    raw: str

    def as_dict(self) -> dict[str, Any]:
        if len(self.clauses) == 1:
            return self.clauses[0].as_dict()
        return {"any": [clause.as_dict() for clause in self.clauses]}

    def describe(self) -> str:
        return " or ".join(
            f"{clause.path} {clause.op} {clause.threshold}" for clause in self.clauses
        )


def parse_condition(text: Any) -> ConditionGroup:
    """Parse ``quote.change_pct >= 9.8 or quote.change_pct <= -9.8``."""
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("无法解析告警条件：条件为空；格式应为 field.path op value [or ...]")
    clauses: list[Condition] = []
    for part in _OR.split(raw):
        chunk = part.strip()
        match = _CONDITION.match(chunk)
        if not match:
            raise ValueError(f"无法解析告警条件 {chunk!r}；格式应为 field.path op value [or ...]")
        path, op, literal = match.group(1), match.group(2), match.group(3)
        clauses.append(Condition(path=path, op=op, threshold=_literal(literal), raw=chunk))
    return ConditionGroup(tuple(clauses), raw)


def _literal(text: str) -> float | str | bool | None:
    value = text.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"none", "null"}:
        return None
    if _NUMBER.match(value):
        return float(value)
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compare(value: Any, op: str, threshold: Any) -> bool | None:
    """Return the comparison result, or ``None`` when it cannot be decided."""
    if op not in _OPERATORS or value is None:
        return None
    if isinstance(threshold, bool):
        if op not in {"==", "!="}:
            return None
        return (bool(value) == threshold) if op == "==" else (bool(value) != threshold)
    if isinstance(threshold, str):
        if op not in {"==", "!="}:
            return None
        return (str(value) == threshold) if op == "==" else (str(value) != threshold)
    left = _number(value)
    right = _number(threshold)
    if left is None or right is None:
        return None
    return {
        ">=": left >= right,
        "<=": left <= right,
        "==": left == right,
        "!=": left != right,
        ">": left > right,
        "<": left < right,
    }[op]


def evaluate(condition: ConditionGroup, facts: Mapping[str, Any]) -> tuple[bool | None, Any]:
    """Return ``(fired, observed)``; ``fired`` is ``None`` when data is absent.

    Clauses combine as a three-valued ``or``: any decided ``True`` wins, an
    otherwise undecidable clause keeps the result unknown, all ``False`` means
    the rule did not fire.
    """
    values = [facts.get(clause.path) for clause in condition.clauses]
    decisions = [
        compare(value, clause.op, clause.threshold)
        for value, clause in zip(values, condition.clauses, strict=False)
    ]
    if any(decision is True for decision in decisions):
        fired: bool | None = True
    elif any(decision is None for decision in decisions):
        fired = None
    else:
        fired = False
    observed = values[0] if len(values) == 1 else values
    return fired, observed


def referenced_domains(rules: Iterable[Mapping[str, Any]]) -> list[str]:
    """Domains a rule set actually reads, in first-seen order."""
    domains: list[str] = []
    for rule in rules:
        try:
            condition = parse_condition(rule.get("when"))
        except ValueError:
            continue
        for clause in condition.clauses:
            prefix = clause.path.split(".", 1)[0]
            if prefix != "data_envelope" and prefix not in domains:
                domains.append(prefix)
    return domains


# --------------------------------------------------------------------------- #
# facts
# --------------------------------------------------------------------------- #


def build_facts(envelopes: Mapping[str, DataEnvelope]) -> dict[str, Any]:
    """Flatten the envelopes into the dotted paths rules can name."""
    facts: dict[str, Any] = {}
    overall = summarize(envelopes)
    facts["data_envelope.data_source_status"] = overall["status"]
    for name, envelope in envelopes.items():
        facts[f"data_envelope.{name}.data_source_status"] = envelope.data_source_status.value

    quote = envelopes.get("quote")
    if quote is not None and quote.usable:
        payload = _first_row(quote.payload)
        change = _first_number(
            payload, ("change_pct", "pct_chg", "pct_change", "changePercent", "涨跌幅")
        )
        price = _first_number(payload, ("price", "close", "latest", "last", "最新价"))
        if change is not None:
            facts["quote.change_pct"] = change
        if price is not None:
            facts["quote.price"] = price

    kline = envelopes.get("kline")
    if kline is not None and kline.usable:
        rows = [
            row
            for row in rows_of(kline.payload)
            if _first_number(row, ("close", "收盘", "price", "last")) is not None
        ]
        rows = sorted(rows, key=lambda row: _first_text(row, ("date", "trade_date", "day")))
        if rows:
            facts["kline.bars"] = len(rows)
            facts["kline.close"] = _first_number(rows[-1], ("close", "收盘", "price", "last"))
            volumes = [
                value
                for value in (_first_number(row, ("volume", "vol", "成交量")) for row in rows)
                if value is not None
            ]
            if len(volumes) >= 6:
                baseline = sum(volumes[-6:-1]) / 5
                if baseline > 0:
                    facts["kline.volume_ratio_5d"] = volumes[-1] / baseline
            elif len(volumes) >= 2:
                baseline = sum(volumes[:-1]) / (len(volumes) - 1)
                if baseline > 0:
                    facts["kline.volume_ratio_5d"] = volumes[-1] / baseline

    risk = envelopes.get("risk_event")
    if risk is not None and risk.usable:
        risk_rows, risk_payload = _payload_rows(risk.payload, _RISK_LIST_KEYS)
        if risk_rows is None:
            risk_rows = [risk_payload] if risk_payload else []
        severity, count = _risk_severity(risk_rows)
        facts["risk_event.count"] = count
        if severity:
            facts["risk_event.severity"] = severity
        elif not risk_rows:
            # The provider scanned the announcements and found no risk event;
            # that is a decided "none", not missing data.
            facts["risk_event.severity"] = "none"

    lockup = envelopes.get("lockup_calendar")
    if lockup is not None and lockup.usable:
        lockup_rows, lockup_payload = _payload_rows(lockup.payload, _LOCKUP_LIST_KEYS)
        window = _first_number(lockup_payload, ("forward_days", "horizon_days"))
        if lockup_rows is None:
            lockup_rows = [lockup_payload] if lockup_payload else []
        days, count = _days_to_unlock(lockup_rows, _as_day(lockup))
        facts["lockup_calendar.count"] = count
        if days is not None:
            facts["lockup_calendar.days_to_expiry"] = days
        elif not lockup_rows and window is not None:
            # ``upcoming`` is empty over a declared forward window: the nearest
            # unlock is beyond it. Report the window bound, never a made-up date.
            facts["lockup_calendar.days_to_expiry"] = float(window + 1)
        if window is not None:
            facts["lockup_calendar.window_days"] = float(window)

    return facts


def _payload_rows(
    payload: Any, list_keys: Iterable[str]
) -> tuple[list[Mapping[str, Any]] | None, Mapping[str, Any]]:
    """Return ``(rows, flat payload)``; ``rows`` is ``None`` when no list exists."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)], {}
    if isinstance(payload, Mapping):
        for key in list_keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, Mapping)], payload
        return None, payload
    return None, {}


def _first_row(payload: Any) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        return payload
    if isinstance(payload, list):
        for row in payload:
            if isinstance(row, Mapping):
                return row
    return {}


def _as_day(envelope: DataEnvelope) -> date | None:
    try:
        return date.fromisoformat(envelope.as_of_date[:10])
    except (TypeError, ValueError):
        return None


def _risk_severity(rows: list[Mapping[str, Any]]) -> tuple[str | None, int]:
    count = 0
    highest = 0
    for row in rows:
        title = _first_text(row, _TITLE_KEYS)
        explicit = _first_text(row, _SEVERITY_KEYS).lower()
        if not title and not explicit:
            continue
        count += 1
        if explicit in {"high", "mid", "medium", "low", "高", "中", "低"}:
            mapped = {
                "high": 3,
                "mid": 2,
                "medium": 2,
                "low": 1,
                "高": 3,
                "中": 2,
                "低": 1,
            }[explicit]
        elif any(word in title for word in _HIGH_RISK):
            mapped = 3
        elif any(word in title for word in _MID_RISK):
            mapped = 2
        else:
            mapped = 1
        highest = max(highest, mapped)
    if not count:
        return None, 0
    return {3: "high", 2: "mid", 1: "low"}[highest], count


def _days_to_unlock(rows: list[Mapping[str, Any]], base: date | None) -> tuple[int | None, int]:
    days: list[date] = []
    for row in rows:
        text = _first_text(row, _DATE_KEYS)
        try:
            days.append(date.fromisoformat(text[:10]))
        except (TypeError, ValueError):
            continue
    if not days:
        return None, 0
    reference = base or date.today()
    upcoming = sorted(day for day in days if day >= reference)
    return ((upcoming[0] - reference).days if upcoming else None), len(days)


def _first_number(row: Mapping[str, Any], keys: Iterable[str]) -> float | None:
    for key in keys:
        if key in row:
            value = _number(row[key])
            if value is not None:
                return value
    return None


def _first_text(row: Mapping[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return ""


__all__ = [
    "Condition",
    "ConditionGroup",
    "build_facts",
    "compare",
    "evaluate",
    "parse_condition",
    "referenced_domains",
]
