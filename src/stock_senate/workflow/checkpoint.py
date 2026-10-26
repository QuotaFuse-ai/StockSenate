"""Stage-level checkpoints so an interrupted run can resume.

A checkpoint records the output of every completed stage under
``(code, date, config_hash)``. A resume restores those outputs instead of
calling the node again, so a run that died halfway through an expensive
debate does not pay for the completed analysts a second time.

The store is deliberately dumb: one SQLite table, JSON payloads, no
migration. Rows for a key are deleted when the run reaches its end, so a
finished analysis is never served from a stale checkpoint.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stock_senate.data.envelope import now_iso

log = logging.getLogger("stock_senate.checkpoint")

SUPPORTED_BACKENDS = ("sqlite",)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    run_key TEXT NOT NULL,
    stage TEXT NOT NULL,
    payload TEXT NOT NULL,
    budget TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_key, stage)
)
"""


def run_key(code: str, date: str, config_hash: str) -> str:
    return f"{code}|{date}|{config_hash}"


@dataclass
class CheckpointState:
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""

    def __bool__(self) -> bool:
        return bool(self.stages)

    @property
    def names(self) -> list[str]:
        return sorted(self.stages)


class CheckpointStore:
    """SQLite-backed stage store keyed by code, date and config hash."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute(_SCHEMA)
        return connection

    def load(self, *, code: str, date: str, config_hash: str) -> CheckpointState:
        key = run_key(code, date, config_hash)
        state = CheckpointState()
        try:
            with closing(self._connect()) as connection, connection:
                rows = connection.execute(
                    "SELECT stage, payload, budget, updated_at FROM checkpoints"
                    " WHERE run_key = ? ORDER BY updated_at",
                    (key,),
                ).fetchall()
        except sqlite3.Error as exc:
            log.warning("checkpoint read failed, starting fresh: %s", exc)
            return state
        for stage, payload, budget, updated_at in rows:
            try:
                state.stages[stage] = json.loads(payload)
                state.budget = json.loads(budget) or state.budget
            except (TypeError, ValueError):
                # A corrupt row must not poison the run; the stage just reruns.
                log.warning("checkpoint row %s is unreadable, ignoring it", stage)
                continue
            if str(updated_at) > state.updated_at:
                state.updated_at = str(updated_at)
        return state

    def save(
        self,
        *,
        code: str,
        date: str,
        config_hash: str,
        stage: str,
        payload: Mapping[str, Any],
        budget: Mapping[str, Any] | None = None,
    ) -> None:
        key = run_key(code, date, config_hash)
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "INSERT OR REPLACE INTO checkpoints"
                    " (run_key, stage, payload, budget, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        key,
                        stage,
                        json.dumps(payload, ensure_ascii=False, default=str),
                        json.dumps(dict(budget or {}), ensure_ascii=False, default=str),
                        now_iso(),
                    ),
                )
        except (sqlite3.Error, TypeError, ValueError) as exc:
            # Checkpoints are an optimisation; losing one is never fatal.
            log.warning("checkpoint write failed at %s: %s", stage, exc)

    def clear(self, *, code: str, date: str, config_hash: str) -> None:
        key = run_key(code, date, config_hash)
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("DELETE FROM checkpoints WHERE run_key = ?", (key,))
        except sqlite3.Error as exc:
            log.warning("checkpoint cleanup failed: %s", exc)


def store_from_config(
    config: Mapping[str, Any], home: Path | None = None
) -> CheckpointStore | None:
    """Return the configured store, or ``None`` when checkpoints are off."""
    section = (((config or {}).get("workflow") or {}).get("checkpoint")) or {}
    if section.get("enabled") is not True:
        return None
    backend = str(section.get("backend") or "sqlite").lower()
    if backend not in SUPPORTED_BACKENDS:
        log.warning(
            "checkpoint backend %r is not supported; checkpoints stay off", backend
        )
        return None
    raw = str(section.get("path") or "").strip()
    if not raw:
        base = Path(home) if home else Path(str((config or {}).get("home") or "~"))
        target = base.expanduser() / "checkpoints.sqlite"
    else:
        target = Path(raw).expanduser()
        if not target.is_absolute():
            base = Path(home) if home else Path(str((config or {}).get("home") or "~"))
            target = base.expanduser() / target
    return CheckpointStore(target)


__all__ = [
    "CheckpointState",
    "CheckpointStore",
    "SUPPORTED_BACKENDS",
    "run_key",
    "store_from_config",
]
