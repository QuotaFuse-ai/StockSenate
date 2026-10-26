"""Shared helpers for subprocess CLI tests.

Every fixture below is generated locally so the CLI can run with
``mode = "local"`` and ``llm_provider = "offline"``: no socket, no model, no
network. The archive layout mirrors the shipped ``local_json`` provider.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CONFIG = ROOT / "config"

CODE = "600519"
DAY = "2026-01-28"
START = "2025-12-01"

DOMAINS = (
    "kline",
    "quote",
    "financial",
    "announcement",
    "research",
    "risk_event",
    "news",
    "social_buzz",
    "policy_feed",
    "money_flow",
    "dragon_tiger",
    "lockup_calendar",
)

_LEGACY_VARS = (
    "QF_COUNCIL_CONFIG_DIR",
    "QF_COUNCIL_HOME",
    "TRADINGAGENTS_CONFIG_DIR",
    "TRADINGAGENTS_HOME",
)


def cli_env(
    *,
    home: Path | None = None,
    config_dir: Path | None = None,
    data_mode: str = "local",
) -> dict[str, str]:
    """A copy of the environment forced onto the offline/local path."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["STOCKSENATE_LLM_PROVIDER"] = "offline"
    env["STOCKSENATE_DATA_MODE"] = data_mode
    if home is not None:
        env["STOCKSENATE_HOME"] = str(home)
    if config_dir is not None:
        env["STOCKSENATE_CONFIG_DIR"] = str(config_dir)
    for key in _LEGACY_VARS:
        env.pop(key, None)
    return env


def run_cli(
    *args: str,
    env: dict[str, str] | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "stock_senate", *args],
        cwd=ROOT,
        env=env or cli_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def parse_json(result: subprocess.CompletedProcess[str]) -> Any:
    """Parse stdout as one JSON document; stderr must stay out of it."""
    return json.loads(result.stdout)


def kline_bars(days: int = 59, first: str = START) -> list[dict[str, Any]]:
    """Rise for 30 sessions then fall, so buy and sell signals both appear."""
    start = date.fromisoformat(first)
    rising = [10.0 + 10.0 * index / 29 for index in range(30)]
    falling = [20.0 - 12.0 * (index + 1) / 29 for index in range(days - 30)]
    rows: list[dict[str, Any]] = []
    previous: float | None = None
    for index, close in enumerate(rising + falling):
        open_ = close if previous is None else round((previous + close) / 2, 4)
        rows.append(
            {
                "date": (start + timedelta(days=index)).isoformat(),
                "open": open_,
                "high": round(max(open_, close) * 1.005, 4),
                "low": round(min(open_, close) * 0.995, 4),
                "close": round(close, 4),
                "volume": 1_000_000,
            }
        )
        previous = close
    return rows


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def write_archive(
    root: Path,
    *,
    domains: Iterable[str] = DOMAINS,
    code: str = CODE,
    day: str = DAY,
    bars: list[dict[str, Any]] | None = None,
) -> Path:
    """Write one archive record per domain, laid out ``<root>/<domain>/<code>/<day>.json``."""
    for domain in domains:
        if domain == "kline":
            payload: dict[str, Any] = {
                "code": code,
                "period": "daily",
                "klines": bars if bars is not None else kline_bars(),
            }
        else:
            payload = {
                "code": code,
                "items": [{"title": f"{domain} sample", "summary": "sample", "value": 1.0}],
            }
        _write_json(root / domain / code / f"{day}.json", {"data": payload, "data_as_of": day})
    return root


def write_config_dir(directory: Path, archive_root: Path) -> Path:
    """A one-file override directory; the packaged config supplies the rest."""
    directory.mkdir(parents=True, exist_ok=True)
    domains = ", ".join(json.dumps(domain) for domain in DOMAINS)
    routing = "\n".join(f'{domain} = ["local_json"]' for domain in DOMAINS)
    text = (
        "[global]\n"
        'mode = "local"\n'
        "allow_fallback = true\n"
        "forbid_llm_fill = true\n"
        "\n"
        "[providers.local_json]\n"
        'kind = "json_file"\n'
        f'root = "{archive_root.as_posix()}"\n'
        'layout = "{domain}/{code}/{date}.json"\n'
        f"domains = [{domains}]\n"
        "\n"
        "[routing]\n"
        f"{routing}\n"
        "\n"
        "[freshness]\n"
        "enforce_no_lookahead = true\n"
    )
    (directory / "data_source.toml").write_text(text, encoding="utf-8")
    return directory


def walk_keys(payload: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            keys.add(str(key))
            keys |= walk_keys(value)
    elif isinstance(payload, list):
        for item in payload:
            keys |= walk_keys(item)
    return keys
