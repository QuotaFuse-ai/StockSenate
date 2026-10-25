"""Smoke tests for the public CLI surface.

These assert the contract users see, not internals: command names, exit codes
and the stdout/stderr split documented in docs/specs/cli-reference.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def python_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "stock_senate", *args],
        cwd=ROOT,
        env=python_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


def test_version() -> None:
    result = run_cli("version")
    assert result.returncode == 0
    assert result.stdout.strip().startswith("stocksenate ")


def test_help_lists_documented_commands() -> None:
    result = run_cli("--help")
    assert result.returncode == 0
    for command in (
        "analyze",
        "backtest",
        "alert",
        "report",
        "queue",
        "role",
        "mcp",
        "webui",
        "config",
        "performance",
        "sync-news",
    ):
        assert command in result.stdout


def test_config_validate_repository_config() -> None:
    result = run_cli("config", "validate", "--config", "config", "--output", "json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True


def test_missing_config_dir_is_a_config_error() -> None:
    result = run_cli("config", "validate", "--config", "does-not-exist", "--output", "json")
    assert result.returncode == 3
    assert json.loads(result.stdout)["ok"] is False


def test_role_list_has_sixteen_keys() -> None:
    result = run_cli("role", "list", "--config", "config", "--output", "json")
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    assert len(rows) == 16
    assert {row["role"] for row in rows} >= {"market", "trader", "portfolio_manager"}


def test_unknown_role_is_a_config_error() -> None:
    result = run_cli("role", "show", "nope", "--config", "config", "--output", "json")
    assert result.returncode == 3


def test_legacy_entry_point_warns_on_stderr() -> None:
    script = (
        "import sys;"
        "sys.argv=['tradingagents','version'];"
        "from stock_senate.cli import legacy_main;"
        "legacy_main()"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=python_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "tradingagents" in result.stderr
    assert "ssenate" in result.stderr
    assert result.stdout.strip().startswith("stocksenate ")
