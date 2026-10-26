"""Regression matrix for the documented CLI exit codes.

Contract (``src/stock_senate/cli.py``): ``0`` success · ``2`` bad arguments ·
``3`` bad configuration · ``4`` data unavailable · ``7`` rate limited ·
``8`` partial failure. Each code has at least one black-box case.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from cli_support import (
    CODE,
    CONFIG,
    DAY,
    START,
    cli_env,
    parse_json,
    run_cli,
    write_archive,
    write_config_dir,
)
from typer.testing import CliRunner

import stock_senate.cli as cli
from stock_senate.graph.senate_graph import SenateGraph
from stock_senate.roles.llm import LLMRateLimited


def full_env(tmp_path: Path, *, empty_archive: bool = False):
    archive = tmp_path / "archive"
    if not empty_archive:
        write_archive(archive)
    config = write_config_dir(tmp_path / "config", archive)
    return cli_env(home=tmp_path / "home", config_dir=config)


# --------------------------------------------------------------------------- #
# 0 — success
# --------------------------------------------------------------------------- #


def test_version_exits_0() -> None:
    result = run_cli("version")

    assert result.returncode == 0
    assert result.stdout.startswith("stocksenate ")
    assert result.stderr == ""


def test_help_exits_0() -> None:
    result = run_cli("--help")

    assert result.returncode == 0
    assert "analyze" in result.stdout


# --------------------------------------------------------------------------- #
# 2 — bad arguments
# --------------------------------------------------------------------------- #


def test_missing_analyze_argument_exits_2() -> None:
    result = run_cli("analyze")

    assert result.returncode == 2
    assert result.stdout == ""
    assert "Missing argument" in result.stderr


def test_backtest_without_targets_exits_2() -> None:
    result = run_cli("backtest", "run", "--start", START, "--end", DAY)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "backtest run 需要 --code 或 --codes" in result.stderr


def test_backtest_missing_required_range_exits_2() -> None:
    result = run_cli("backtest", "run", "--code", CODE)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "--start" in result.stderr


def test_backtest_zero_window_exits_2(tmp_path: Path) -> None:
    result = run_cli(
        "backtest",
        "run",
        "--code",
        CODE,
        "--start",
        START,
        "--end",
        DAY,
        "--window",
        "0",
        env=full_env(tmp_path),
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "window" in result.stderr


def test_backtest_inverted_range_exits_2(tmp_path: Path) -> None:
    result = run_cli(
        "backtest",
        "run",
        "--code",
        CODE,
        "--start",
        DAY,
        "--end",
        START,
        env=full_env(tmp_path),
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "--start 不能晚于 --end" in result.stderr


def test_backtest_unknown_option_exits_2(tmp_path: Path) -> None:
    """``backtest run`` accepts ``--config`` (cli-reference §2.3); other flags still fail."""
    result = run_cli(
        "backtest",
        "run",
        "--code",
        CODE,
        "--start",
        START,
        "--end",
        DAY,
        "--not-an-option",
        "x",
        env=full_env(tmp_path),
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "No such option" in result.stderr


# --------------------------------------------------------------------------- #
# 3 — bad configuration
# --------------------------------------------------------------------------- #


def test_unknown_role_exits_3() -> None:
    result = run_cli("role", "show", "ghost", "--config", str(CONFIG))

    assert result.returncode == 3
    assert result.stdout == ""
    assert "未知角色" in result.stderr


def test_forbid_llm_fill_false_is_a_config_error(tmp_path: Path) -> None:
    directory = tmp_path / "config"
    shutil.copytree(CONFIG, directory)
    path = directory / "data_source.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "forbid_llm_fill = true", "forbid_llm_fill = false"
        ),
        encoding="utf-8",
    )

    result = run_cli("config", "validate", "--config", str(directory), "--output", "json")

    assert result.returncode == 3
    report = parse_json(result)
    assert report["ok"] is False
    assert any("forbid_llm_fill" in error["key"] for error in report["errors"])


# --------------------------------------------------------------------------- #
# 4 — data unavailable
# --------------------------------------------------------------------------- #


def test_analyze_without_data_exits_4(tmp_path: Path) -> None:
    result = run_cli(
        "analyze",
        CODE,
        "--date",
        DAY,
        "--output",
        "json",
        env=full_env(tmp_path, empty_archive=True),
    )

    assert result.returncode == 4
    decision = parse_json(result)["decision"]
    assert decision["data"]["status"] == "missing"


def test_backtest_without_data_exits_4(tmp_path: Path) -> None:
    result = run_cli(
        "backtest",
        "run",
        "--code",
        CODE,
        "--start",
        START,
        "--end",
        DAY,
        "--output",
        "json",
        env=full_env(tmp_path, empty_archive=True),
    )

    assert result.returncode == 4
    assert parse_json(result)["data"]["status"] == "missing"


# --------------------------------------------------------------------------- #
# 8 — partial failure
# --------------------------------------------------------------------------- #


def test_partial_backtest_exits_8(tmp_path: Path) -> None:
    result = run_cli(
        "backtest",
        "run",
        "--codes",
        f"{CODE},000001",
        "--start",
        START,
        "--end",
        DAY,
        "--output",
        "json",
        env=full_env(tmp_path),
    )

    assert result.returncode == 8
    payload = parse_json(result)
    assert payload["partial"] is True
    assert payload["data"]["missing_codes"] == ["000001"]


# --------------------------------------------------------------------------- #
# 7 — rate limited
# --------------------------------------------------------------------------- #


def test_rate_limit_constant_is_declared() -> None:
    assert cli.EXIT_RATE_LIMIT == 7


def test_rate_limited_analyze_exits_7(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self, *args, **kwargs):  # noqa: ANN001 - patched boundary
        raise LLMRateLimited("429 too many requests")

    monkeypatch.setattr(SenateGraph, "propagate", boom)
    result = CliRunner().invoke(cli.app, ["analyze", CODE, "--date", DAY])

    assert result.exit_code == 7


# --------------------------------------------------------------------------- #
# argument and configuration classification inside analyze
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["2026-13-99", "not-a-date"])
def test_malformed_date_is_a_usage_error(tmp_path: Path, value: str) -> None:
    result = run_cli(
        "analyze",
        CODE,
        "--date",
        value,
        "--output",
        "json",
        env=full_env(tmp_path),
    )

    assert result.returncode == 2


def test_unknown_disable_role_is_a_config_error(tmp_path: Path) -> None:
    result = run_cli(
        "analyze",
        CODE,
        "--date",
        DAY,
        "--disable",
        "ghost",
        "--output",
        "json",
        env=full_env(tmp_path),
    )

    assert result.returncode == 3


# --------------------------------------------------------------------------- #
# stream discipline
# --------------------------------------------------------------------------- #


def test_usage_errors_write_stderr_only() -> None:
    result = run_cli("backtest", "run", "--start", START, "--end", DAY)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.strip()


def test_json_success_keeps_stdout_parseable(tmp_path: Path) -> None:
    result = run_cli("analyze", CODE, "--date", DAY, "--output", "json", env=full_env(tmp_path))

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["decision"]["execution"] is False
