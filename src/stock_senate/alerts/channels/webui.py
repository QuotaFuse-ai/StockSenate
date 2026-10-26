"""WebUI channel: append the alert to ``<home>/alerts/alerts.jsonl``.

The WebUI reads this log; nothing here talks to the WebUI process directly,
so the alert loop keeps working when the UI is down.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from stock_senate.config.loader import home_dir


def alerts_log(home: Path | str | None = None) -> Path:
    root = Path(home).expanduser() if home else home_dir()
    return root / "alerts" / "alerts.jsonl"


def send(
    event: Mapping[str, Any], config: Mapping[str, Any], *, home: Path | str | None = None
) -> dict[str, Any]:
    path = alerts_log(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = dict(event)
    record.setdefault("delivered_at", datetime.now().isoformat(timespec="seconds"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return {"channel": "webui", "ok": True, "path": str(path)}


def recent(limit: int = 50, *, home: Path | str | None = None) -> list[dict[str, Any]]:
    path = alerts_log(home)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-max(1, int(limit)) :]


__all__ = ["alerts_log", "recent", "send"]
