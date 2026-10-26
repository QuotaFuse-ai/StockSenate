"""Smoke tests for the public CLI surface.

These assert the contract users see, not internals: command names, exit codes
and the stdout/stderr split documented in docs/specs/cli-reference.md.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_version_flag_matches_version_command() -> None:
    flag = run_cli("--version")
    command = run_cli("version")
    assert flag.returncode == 0
    assert flag.stdout == command.stdout


def test_stocksenate_prefix_wins_over_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    from stock_senate import cli

    monkeypatch.setenv("STOCKSENATE_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("TRADINGAGENTS_CONFIG_DIR", str(ROOT / "ignored-a"))
    monkeypatch.setenv("QF_COUNCIL_CONFIG_DIR", str(ROOT / "ignored-b"))

    assert cli._config_dir() == ROOT / "config"


def test_tradingagents_precedes_qf_council(monkeypatch: pytest.MonkeyPatch) -> None:
    from stock_senate import cli

    monkeypatch.delenv("STOCKSENATE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("TRADINGAGENTS_CONFIG_DIR", str(ROOT / "legacy-a"))
    monkeypatch.setenv("QF_COUNCIL_CONFIG_DIR", str(ROOT / "legacy-b"))

    assert cli._config_dir() == ROOT / "legacy-a"


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


def test_config_validate_strict_rejects_unknown_keys(tmp_path: Path) -> None:
    directory = tmp_path / "config"
    shutil.copytree(ROOT / "config", directory)
    path = directory / "workflow.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "max_debate_rounds = 2",
            "max_debate_rounds = 2\nmax_debate_roundz = 3",
            1,
        ),
        encoding="utf-8",
    )

    lenient = run_cli("config", "validate", "--config", str(directory), "--output", "json")
    assert lenient.returncode == 0, lenient.stderr
    assert json.loads(lenient.stdout)["ok"] is True

    strict = run_cli(
        "config", "validate", "--config", str(directory), "--strict", "--output", "json"
    )
    assert strict.returncode == 3
    payload = json.loads(strict.stdout)
    assert payload["ok"] is False
    assert any(error["key"] == "debate.max_debate_roundz" for error in payload["errors"])


def test_config_show_does_not_echo_secrets_or_absolute_paths(tmp_path: Path) -> None:
    sentinel = "sk-audit-sentinel-0000"
    (tmp_path / "llm_config.toml").write_text(
        "[llm]\n"
        f'api_key = "{sentinel}"\n'
        'dsn = "postgres://user:pass@localhost/db"\n',
        encoding="utf-8",
    )
    (tmp_path / "data_source.toml").write_text(
        "[providers.local_json]\n"
        'kind = "json_file"\n'
        f'root = "{(tmp_path / "archive").as_posix()}"\n',
        encoding="utf-8",
    )

    result = run_cli("config", "show", "--config", str(tmp_path), "--output", "json")

    assert result.returncode == 0, result.stderr
    assert sentinel not in result.stdout
    assert sentinel not in result.stderr
    assert str(tmp_path) not in result.stdout
    assert "postgres://user:pass@localhost/db" not in result.stdout
    payload = json.loads(result.stdout)
    assert payload["llm_config"]["llm"]["api_key"] == "***"
    assert payload["llm_config"]["llm"]["dsn"] == "***"
    assert payload["config_dir"] == "***"
    assert payload["data_source"]["providers"]["local_json"]["root"] == "***"


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
