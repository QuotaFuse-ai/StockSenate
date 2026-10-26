"""Backtest data model: seven tables, one SQLite file per run.

The tables mirror the PRD §12 contract. They are created on every run so a
result directory is self-describing: a reviewer can open ``backtest.sqlite``
and answer "which signal, which fill, which equity point" without parsing the
JSON blob.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TABLES = (
    "decision_record",
    "backtest_run",
    "trade",
    "position",
    "metric",
    "price_series",
    "benchmark_series",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS backtest_run (
    run_key TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    window INTEGER NOT NULL,
    slippage REAL NOT NULL,
    fee REAL NOT NULL,
    codes_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decision_record (
    run_key TEXT NOT NULL,
    code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    signal INTEGER NOT NULL,
    source TEXT NOT NULL,
    rating TEXT
);
CREATE TABLE IF NOT EXISTS trade (
    run_key TEXT NOT NULL,
    code TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_date TEXT NOT NULL,
    exit_price REAL NOT NULL,
    shares INTEGER NOT NULL,
    fees REAL NOT NULL,
    pnl REAL NOT NULL,
    return_pct REAL NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS position (
    run_key TEXT NOT NULL,
    code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    shares INTEGER NOT NULL,
    cash REAL NOT NULL,
    equity REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS metric (
    run_key TEXT NOT NULL,
    name TEXT NOT NULL,
    value_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_series (
    run_key TEXT NOT NULL,
    code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL
);
CREATE TABLE IF NOT EXISTS benchmark_series (
    run_key TEXT NOT NULL,
    code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    close REAL
);
CREATE INDEX IF NOT EXISTS idx_decision_run ON decision_record(run_key);
CREATE INDEX IF NOT EXISTS idx_trade_run ON trade(run_key);
CREATE INDEX IF NOT EXISTS idx_position_run ON position(run_key);
CREATE INDEX IF NOT EXISTS idx_price_run ON price_series(run_key);
CREATE INDEX IF NOT EXISTS idx_benchmark_run ON benchmark_series(run_key);
"""


@dataclass(frozen=True)
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Trade:
    code: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    shares: int
    fees: float
    pnl: float
    return_pct: float
    reason: str


class BacktestStore:
    """Thin writer over one SQLite file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._connection = sqlite3.connect(self.path)
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def write_run(self, run: dict[str, Any]) -> None:
        key = str(run["run_key"])
        connection = self._connection
        connection.execute(
            "INSERT OR REPLACE INTO backtest_run VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                run.get("started_at", ""),
                run.get("start", ""),
                run.get("end", ""),
                int(run.get("window", 1)),
                float(run.get("slippage", 0.0)),
                float(run.get("fee", 0.0)),
                json.dumps(run.get("codes", []), ensure_ascii=False),
            ),
        )
        for name in ("decision_record", "trade", "position", "price_series", "benchmark_series"):
            connection.execute(f"DELETE FROM {name} WHERE run_key = ?", (key,))
        connection.execute("DELETE FROM metric WHERE run_key = ?", (key,))

        connection.executemany(
            "INSERT INTO decision_record VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    key,
                    row["code"],
                    row["date"],
                    int(row["signal"]),
                    row["source"],
                    row.get("rating"),
                )
                for row in run.get("decisions", [])
            ],
        )
        connection.executemany(
            "INSERT INTO trade VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    key,
                    row["code"],
                    row["entry_date"],
                    row["entry_price"],
                    row["exit_date"],
                    row["exit_price"],
                    int(row["shares"]),
                    row["fees"],
                    row["pnl"],
                    row["return_pct"],
                    row["reason"],
                )
                for row in run.get("trades", [])
            ],
        )
        connection.executemany(
            "INSERT INTO position VALUES (?, ?, ?, ?, ?, ?)",
            [
                (key, row["code"], row["date"], int(row["shares"]), row["cash"], row["equity"])
                for row in run.get("positions", [])
            ],
        )
        connection.executemany(
            "INSERT INTO price_series VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    key,
                    row["code"],
                    row["date"],
                    row["open"],
                    row["high"],
                    row["low"],
                    row["close"],
                    row["volume"],
                )
                for row in run.get("prices", [])
            ],
        )
        connection.executemany(
            "INSERT INTO benchmark_series VALUES (?, ?, ?, ?)",
            [
                (key, row["code"], row["date"], row["close"])
                for row in run.get("benchmark_rows", [])
            ],
        )
        connection.executemany(
            "INSERT INTO metric VALUES (?, ?, ?)",
            [
                (key, name, json.dumps(value, ensure_ascii=False, default=str))
                for name, value in run.get("metric_rows", [])
            ],
        )
        connection.commit()

    def table_counts(self) -> dict[str, int]:
        counts = {}
        for table in TABLES:
            counts[table] = self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return counts

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> BacktestStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def write_tables(path: Path | str, run: dict[str, Any]) -> dict[str, int]:
    with BacktestStore(path) as store:
        store.write_run(run)
        return store.table_counts()


__all__ = ["TABLES", "Bar", "BacktestStore", "Trade", "write_tables"]
