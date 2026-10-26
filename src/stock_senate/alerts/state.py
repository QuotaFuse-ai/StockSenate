"""Alert state machine: dedup, cooldown, daily cap, quiet hours, recovery.

One entry per ``rule_id + code``. The file is rewritten atomically after every
decision so a crash cannot lose or duplicate a notification. State lives at
``<home>/alerts/state.json``; the JSONL delivery log at
``<home>/alerts/alerts.jsonl`` is written by the ``webui`` channel.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

_FORMAT = "%Y-%m-%dT%H:%M:%S"
_ACTIVE = {"triggered", "confirmed", "quiet"}


def in_quiet_hours(moment: datetime, ranges: Iterable[str]) -> bool:
    """``"23:30-07:00"`` wraps midnight; a malformed range is ignored."""
    minutes = moment.hour * 60 + moment.minute
    for spec in ranges or []:
        start_text, _, end_text = str(spec).partition("-")
        start = _minutes(start_text)
        end = _minutes(end_text)
        if start is None or end is None:
            continue
        if start == end or start < end:
            if start <= minutes < end or (start == end):
                return True
        elif minutes >= start or minutes < end:
            return True
    return False


def _minutes(text: str) -> int | None:
    try:
        hour, minute = text.strip().split(":")
        value = int(hour) * 60 + int(minute)
    except (AttributeError, TypeError, ValueError):
        return None
    return value if 0 <= value < 24 * 60 else None


class AlertState:
    def __init__(
        self,
        path: Path | str,
        *,
        dedup_hours: float = 12,
        quiet_hours: Iterable[str] = (),
        max_triggers_per_day: int = 5,
    ) -> None:
        self.path = Path(path)
        self.dedup_minutes = max(0.0, float(dedup_hours) * 60)
        self.quiet_hours = tuple(str(item) for item in quiet_hours or ())
        self.max_per_day = max(1, int(max_triggers_per_day or 1))
        self.entries: dict[str, dict[str, Any]] = {}
        self._load()

    @classmethod
    def from_config(cls, section: Mapping[str, Any], path: Path | str) -> "AlertState":
        global_cfg = dict(section.get("global") or {})
        return cls(
            path,
            dedup_hours=float(global_cfg.get("dedup_hours") or 12),
            quiet_hours=global_cfg.get("quiet_hours") or (),
            max_triggers_per_day=int(global_cfg.get("max_triggers_per_day") or 5),
        )

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        entries = raw.get("rules") if isinstance(raw, dict) else None
        if isinstance(entries, dict):
            self.entries = {
                str(key): dict(value)
                for key, value in entries.items()
                if isinstance(value, dict)
            }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "rules": self.entries}
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    # ------------------------------------------------------------------ #
    # decisions
    # ------------------------------------------------------------------ #

    def decide(
        self,
        rule_id: str,
        code: str,
        *,
        fired: bool | None,
        cooldown_minutes: float | int = 0,
        event: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        """Return ``(status, event_to_deliver)``.

        ``status`` is one of ``triggered`` / ``confirmed`` / ``deduped`` /
        ``suppressed`` / ``quiet`` / ``recovered`` / ``idle`` / ``unknown``.
        Only ``triggered`` and ``confirmed`` carry an event to deliver.
        """
        moment = now or datetime.now()
        key = f"{rule_id}:{code}"
        entry = self.entries.setdefault(key, {})
        previous = str(entry.get("last_status") or "idle")

        if fired is None:
            return "unknown", None
        if not fired:
            if previous in _ACTIVE:
                entry["last_status"] = "recovered"
                entry["recovered_at"] = _stamp(moment)
                self.save()
                return "recovered", None
            return "idle", None

        cooldown = max(float(cooldown_minutes or 0), self.dedup_minutes)
        last = _parse(entry.get("last_fired_at"))
        if last is not None and moment - last < timedelta(minutes=cooldown):
            return "deduped", None

        day = moment.date().isoformat()
        if self._day_total(rule_id, day) >= self.max_per_day:
            entry["last_status"] = "suppressed"
            entry["suppressed_on"] = day
            self.save()
            return "suppressed", None

        if in_quiet_hours(moment, self.quiet_hours):
            pending = entry.setdefault("pending", [])
            if event is not None and not pending:
                pending.append(dict(event))
            entry["last_status"] = "quiet"
            self.save()
            return "quiet", None

        count = int(entry.get("count") or 0) if entry.get("day") == day else 0

        status = "confirmed" if previous in {"triggered", "confirmed"} else "triggered"
        entry.update(
            {
                "last_status": status,
                "last_fired_at": _stamp(moment),
                "day": day,
                "count": count + 1,
            }
        )
        self.save()
        return status, (dict(event) if event is not None else None)

    def flush(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Release alerts deferred during quiet hours; empty while quiet."""
        moment = now or datetime.now()
        if in_quiet_hours(moment, self.quiet_hours):
            return []
        released: list[dict[str, Any]] = []
        for entry in self.entries.values():
            pending = entry.get("pending") or []
            if not pending:
                continue
            released.extend(
                {**dict(item), "released_at": _stamp(moment)} for item in pending
            )
            entry["pending"] = []
            entry["last_status"] = "triggered"
            entry["last_fired_at"] = _stamp(moment)
            day = moment.date().isoformat()
            count = int(entry.get("count") or 0) if entry.get("day") == day else 0
            entry["day"] = day
            entry["count"] = count + len(pending)
        if released:
            self.save()
        return released

    def _day_total(self, rule_id: str, day: str) -> int:
        """Trigger count for the rule across every code today."""
        total = 0
        for key, entry in self.entries.items():
            if key.rsplit(":", 1)[0] == rule_id and entry.get("day") == day:
                total += int(entry.get("count") or 0)
        return total

    def snapshot(self) -> dict[str, Any]:
        return {"path": str(self.path), "entries": self.entries}


def _stamp(moment: datetime) -> str:
    return moment.strftime(_FORMAT)


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


__all__ = ["AlertState", "in_quiet_hours"]
