"""Alert engine: evaluate rules, deduplicate, deliver.

``ssenate alert test`` runs :func:`dry_run` — it reads the rules and the latest
data, reports what would fire, and never pushes or mutates state.

``ssenate alert serve`` runs :func:`serve` — the same evaluation on a timer,
with the state machine in :mod:`stock_senate.alerts.state` deciding whether an
event is new, a confirmation, a duplicate, suppressed by the daily cap, or
deferred by quiet hours.

Watchlist: the codes come from ``STOCKSENATE_ALERT_CODES`` (comma separated)
when set, otherwise from the most recent analysis reports in ``<home>/reports``
— the instruments the operator actually follows. No watchlist file is invented.

Every alert is a reminder and a record. Nothing here places an order, changes a
position, or suggests a price.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from stock_senate.config.loader import ensure_home, load_config
from stock_senate.data.envelope import date_only
from stock_senate.data.router import DataAccessLayer, summarize
from stock_senate.observability.logging import get_logger
from stock_senate.report.store import list_runs

from . import rules as rulebook
from .channels import dispatch
from .state import AlertState

log = get_logger("alerts")

DISCLAIMER = (
    "告警仅做提醒与记录：不下单、不修改持仓，不提供可执行价位、止损位、目标价或仓位，"
    "不构成投资建议。"
)

_ENV_CODES = "STOCKSENATE_ALERT_CODES"


# --------------------------------------------------------------------------- #
# public entry points
# --------------------------------------------------------------------------- #


def dry_run(
    rule_id: str | None = None,
    *,
    config: Mapping[str, Any] | str | Path | None = None,
    home: Path | str | None = None,
    codes: Iterable[str] | None = None,
    as_of: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate the configured rules without pushing or writing state."""
    cfg = load_config(config)
    section = dict(cfg.get("alert_rules") or {})
    moment = now or datetime.now()
    day = date_only(as_of or moment.date().isoformat())
    selected = _select_rules(section, rule_id)
    if selected is None:
        return {
            "ok": False,
            "dry_run": True,
            "error": f"没有 id 为 {rule_id!r} 的告警规则",
            "available": [str(rule.get("id")) for rule in section.get("rules") or []],
        }

    targets = _watchlist(codes=codes, home=home)
    if not targets:
        return {
            "ok": True,
            "dry_run": True,
            "as_of": day,
            "checked_at": moment.isoformat(timespec="seconds"),
            "codes": [],
            "rules": [str(rule.get("id")) for rule in selected],
            "results": [],
            "triggered": 0,
            "unknown": 0,
            "data": {},
            "note": (
                f"没有可监控的标的：先运行 ssenate analyze，或设置 {_ENV_CODES}=600519,000001。"
            ),
            "disclaimer": DISCLAIMER,
        }

    domains = rulebook.referenced_domains(selected)
    facts, data = _collect(cfg, home, targets, day, domains)
    results: list[dict[str, Any]] = []
    for rule in selected:
        results.extend(_evaluate_rule(rule, targets, facts))

    return {
        "ok": True,
        "dry_run": True,
        "as_of": day,
        "checked_at": moment.isoformat(timespec="seconds"),
        "codes": targets,
        "rules": [str(rule.get("id")) for rule in selected],
        "results": results,
        "triggered": sum(1 for row in results if row["status"] == "would_fire"),
        "unknown": sum(1 for row in results if row["status"] == "unknown"),
        "data": data,
        "disclaimer": DISCLAIMER,
    }


def evaluate_once(
    *,
    config: Mapping[str, Any] | str | Path | None = None,
    home: Path | str | None = None,
    codes: Iterable[str] | None = None,
    as_of: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One serving pass: evaluate, apply the state machine, deliver."""
    cfg = load_config(config)
    section = dict(cfg.get("alert_rules") or {})
    moment = now or datetime.now()
    day = date_only(as_of or moment.date().isoformat())
    root = ensure_home(Path(home).expanduser() if home else None)

    global_cfg = dict(section.get("global") or {})
    if global_cfg.get("enabled", True) is False:
        return {"ok": True, "enabled": False, "as_of": day, "delivered": [], "statuses": {}}

    targets = _watchlist(codes=codes, home=root)
    selected = [rule for rule in section.get("rules") or [] if rule.get("enabled", True)]
    if not targets or not selected:
        return {
            "ok": True,
            "enabled": True,
            "as_of": day,
            "codes": targets,
            "delivered": [],
            "statuses": {},
            "note": "没有可监控的标的或已启用的规则。",
        }

    domains = rulebook.referenced_domains(selected)
    facts, data = _collect(cfg, root, targets, day, domains)
    state = AlertState.from_config(section, root / "alerts" / "state.json")

    statuses: dict[str, int] = {}
    delivered: list[dict[str, Any]] = []

    for rule in selected:
        rule_id = str(rule.get("id"))
        for row in _evaluate_rule(rule, targets, facts):
            if row["status"] == "invalid":
                continue
            fired = None if row["status"] == "unknown" else row["status"] == "would_fire"
            event = _event(rule, row, day, moment) if fired else None
            status, outgoing = state.decide(
                rule_id,
                row["code"],
                fired=fired,
                cooldown_minutes=rule.get("cooldown_minutes") or 0,
                event=event,
                now=moment,
            )
            statuses[status] = statuses.get(status, 0) + 1
            if outgoing is not None:
                outgoing["status"] = status
                delivery = dispatch(outgoing, section, home=root)
                delivered.append({"event": outgoing, "delivery": delivery})

    for event in state.flush(moment):
        delivery = dispatch(event, section, home=root)
        delivered.append({"event": event, "delivery": delivery})
        statuses["released"] = statuses.get("released", 0) + 1

    return {
        "ok": True,
        "enabled": True,
        "as_of": day,
        "checked_at": moment.isoformat(timespec="seconds"),
        "codes": targets,
        "statuses": statuses,
        "delivered": delivered,
        "data": data,
        "disclaimer": DISCLAIMER,
    }


def serve(
    interval_seconds: int = 60,
    *,
    config: Mapping[str, Any] | str | Path | None = None,
    home: Path | str | None = None,
    codes: Iterable[str] | None = None,
) -> None:
    """Poll forever until interrupted; each pass is one ``evaluate_once``."""
    interval = max(1, int(interval_seconds or 60))
    log.info("alert serve started: interval=%ss", interval)
    try:
        while True:
            started = time.monotonic()
            try:
                summary = evaluate_once(config=config, home=home, codes=codes)
                log.info(
                    "alert pass",
                    extra={
                        "as_of": summary.get("as_of"),
                        "codes": len(summary.get("codes") or []),
                        "statuses": summary.get("statuses") or {},
                        "delivered": len(summary.get("delivered") or []),
                    },
                )
            except Exception:  # noqa: BLE001 - one bad pass must not stop serving
                log.exception("alert pass failed")
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, interval - elapsed))
    except KeyboardInterrupt:
        log.info("alert serve stopped")


# --------------------------------------------------------------------------- #
# evaluation
# --------------------------------------------------------------------------- #


def _select_rules(section: Mapping[str, Any], rule_id: str | None) -> list[dict[str, Any]] | None:
    rules = [dict(rule) for rule in section.get("rules") or []]
    if not rule_id:
        return rules
    wanted = str(rule_id).strip()
    chosen = [rule for rule in rules if str(rule.get("id")) == wanted]
    return chosen or None


def _evaluate_rule(
    rule: Mapping[str, Any],
    targets: list[str],
    facts: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    try:
        condition = rulebook.parse_condition(rule.get("when"))
    except ValueError as exc:
        return [
            {
                "rule": str(rule.get("id")),
                "name": str(rule.get("name") or rule.get("id")),
                "level": str(rule.get("level") or "mid"),
                "code": code,
                "status": "invalid",
                "reason": str(exc),
                "channels": list(rule.get("channels") or []),
                "condition": str(rule.get("when")),
            }
            for code in targets
        ]

    rows: list[dict[str, Any]] = []
    for code in targets:
        fired, observed = rulebook.evaluate(condition, facts.get(code) or {})
        status = "unknown" if fired is None else ("would_fire" if fired else "not_fired")
        rows.append(
            {
                "rule": str(rule.get("id")),
                "name": str(rule.get("name") or rule.get("id")),
                "level": str(rule.get("level") or "mid"),
                "code": code,
                "status": status,
                "observed": observed,
                "condition": condition.as_dict(),
                "channels": list(rule.get("channels") or []),
            }
        )
    return rows


def _event(
    rule: Mapping[str, Any],
    row: Mapping[str, Any],
    as_of: str,
    moment: datetime,
) -> dict[str, Any]:
    condition = row.get("condition") or {}
    observed = row.get("observed")
    rule_id = str(rule.get("id"))
    code = str(row.get("code"))
    return {
        "id": _event_id(rule_id, code, as_of, moment),
        "rule": {
            "id": rule_id,
            "name": str(rule.get("name") or rule_id),
        },
        "level": str(rule.get("level") or "mid"),
        "code": code,
        "as_of": as_of,
        "status": "triggered",
        "observed": _observed_block(condition, observed),
        "condition": condition,
        "message": (
            f"[{rule.get('level') or 'mid'}] {rule.get('name') or rule_id} {code}: "
            f"{_condition_text(condition)}（实际 {_observed_text(condition, observed)}）"
        ),
        "channels": list(rule.get("channels") or []),
        "created_at": moment.isoformat(timespec="seconds"),
        "disclaimer": DISCLAIMER,
    }


def _condition_text(condition: Mapping[str, Any]) -> str:
    if "any" in condition:
        return " or ".join(
            f"{clause.get('path')} {clause.get('op')} {clause.get('threshold')}"
            for clause in condition["any"]
        )
    return f"{condition.get('path')} {condition.get('op')} {condition.get('threshold')}"


def _observed_block(condition: Mapping[str, Any], observed: Any) -> dict[str, Any]:
    if "any" in condition:
        clauses = list(condition["any"])
        values = observed if isinstance(observed, list) else [observed] * len(clauses)
        return {
            "any": [
                {**clause, "value": value} for clause, value in zip(clauses, values, strict=False)
            ]
        }
    return {**condition, "value": observed}


def _observed_text(condition: Mapping[str, Any], observed: Any) -> str:
    if isinstance(observed, list):
        return " / ".join(str(value) for value in observed)
    return str(observed)


def _event_id(rule_id: str, code: str, as_of: str, moment: datetime) -> str:
    bucket = moment.strftime("%Y-%m-%dT%H")
    seed = f"{rule_id}|{code}|{as_of}|{bucket}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #


def _collect(
    cfg: Mapping[str, Any],
    home: Path | str | None,
    targets: list[str],
    as_of: str,
    domains: list[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    facts: dict[str, dict[str, Any]] = {}
    data: dict[str, dict[str, Any]] = {}
    if not domains:
        domains = ["quote"]
    for code in targets:
        layer = DataAccessLayer(cfg, home=Path(home) if home is not None else None, as_of=as_of)
        envelopes = layer.fetch_many(domains, code, as_of=as_of)
        facts[code] = rulebook.build_facts(envelopes)
        data[code] = {
            **summarize(envelopes),
            "attempts": dict(layer.attempts),
        }
    return facts, data


def _watchlist(*, codes: Iterable[str] | None, home: Path | str | None) -> list[str]:
    explicit = [str(code).strip() for code in codes or [] if str(code).strip()]
    if not explicit:
        import os

        env = os.environ.get(_ENV_CODES, "")
        explicit = [part.strip() for part in env.replace(";", ",").split(",") if part.strip()]
    if explicit:
        return list(dict.fromkeys(explicit))
    seen: list[str] = []
    try:
        rows = list_runs(home=home, limit=200)
    except OSError:
        rows = []
    for row in rows:
        code = str(row.get("code") or "").strip()
        if code and code not in seen:
            seen.append(code)
    return seen


__all__ = ["DISCLAIMER", "dry_run", "evaluate_once", "serve"]
