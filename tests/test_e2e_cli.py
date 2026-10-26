"""End-to-end CLI tests: real subprocess, real files, offline provider.

Every case builds its own archive and configuration directory, then runs
``python -m stock_senate`` exactly as an operator would. ``--output json`` is
used wherever the payload matters, and stdout must always stay parseable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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
    walk_keys,
    write_archive,
    write_config_dir,
)

FORBIDDEN_TRADE_KEYS = {"order_id", "broker_account", "auto_order"}


@dataclass
class Workspace:
    root: Path
    archive: Path
    config: Path
    home: Path

    def env(self, **kwargs):
        return cli_env(home=self.home, config_dir=self.config, **kwargs)


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    archive = write_archive(tmp_path / "archive")
    config = write_config_dir(tmp_path / "config", archive)
    return Workspace(root=tmp_path, archive=archive, config=config, home=tmp_path / "home")


def analyze(ws: Workspace, *extra: str):
    return run_cli(
        "analyze",
        CODE,
        "--date",
        DAY,
        "--config",
        str(ws.config),
        "--output",
        "json",
        *extra,
        env=ws.env(),
    )


def backtest(ws: Workspace, *extra: str, start: str = START, end: str = DAY):
    return run_cli(
        "backtest",
        "run",
        "--code",
        CODE,
        "--start",
        start,
        "--end",
        end,
        "--out-dir",
        str(ws.root / "backtest"),
        "--output",
        "json",
        *extra,
        env=ws.env(),
    )


# --------------------------------------------------------------------------- #
# analyze full flow
# --------------------------------------------------------------------------- #


def test_analyze_full_flow_runs_offline_and_saves_reports(workspace: Workspace) -> None:
    result = analyze(workspace)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    payload = parse_json(result)
    decision = payload["decision"]

    assert payload["code"] == CODE and payload["date"] == DAY
    assert decision["execution"] is False
    assert decision["rating"] in {"buy", "overweight", "hold", "underweight", "sell"}
    assert decision["data"]["status"] == "ok"
    assert decision["data"]["total"] == 12
    assert decision["data"]["ok"] == 12
    assert "不构成投资建议" in decision["disclaimer"]
    assert decision["partial"] is False
    assert decision["quality_score"] >= decision["quality_threshold"]

    for name in ("state", "summary", "report"):
        path = Path(decision["report"][name])
        assert path.is_file(), name
    report_text = Path(decision["report"]["report"]).read_text(encoding="utf-8")
    assert "不构成任何投资建议" in report_text

    assert {"decision", "analysts", "trade", "risk", "quality", "data"} <= set(
        payload["state_keys"]
    )


def test_analyze_never_emits_executable_trade_fields(workspace: Workspace) -> None:
    payload = parse_json(analyze(workspace))
    decision = payload["decision"]

    assert not (walk_keys(decision) & FORBIDDEN_TRADE_KEYS)
    assert decision["action"] in {"买入", "增持", "持有", "减持", "卖出", "未知"}


def test_analyze_json_output_is_a_single_document(workspace: Workspace) -> None:
    result = analyze(workspace)

    assert result.stdout.lstrip().startswith("{")
    json.loads(result.stdout)
    assert "Traceback" not in result.stderr


def test_analyze_empty_archive_exits_4(workspace: Workspace) -> None:
    empty = Workspace(
        root=workspace.root,
        archive=write_archive(workspace.root / "empty" / "archive", domains=[]),
        config=write_config_dir(
            workspace.root / "empty-config", workspace.root / "empty" / "archive"
        ),
        home=workspace.root / "empty-home",
    )

    result = analyze(empty)

    assert result.returncode == 4, result.stderr
    decision = parse_json(result)["decision"]
    assert decision["data"]["status"] == "missing"
    assert decision["execution"] is False


def test_analyze_degraded_bundle_still_succeeds(workspace: Workspace) -> None:
    archive = write_archive(workspace.root / "kline-only", domains=["kline"])
    config = write_config_dir(workspace.root / "kline-only-config", archive)
    degraded = Workspace(workspace.root, archive, config, workspace.root / "kline-only-home")

    result = analyze(degraded)

    assert result.returncode == 0, result.stderr
    decision = parse_json(result)["decision"]
    assert decision["data"]["status"] == "degraded"
    assert decision["data"]["ok"] == 1
    assert len(decision["data"]["missing"]) == 11
    assert decision["partial"] is False


# --------------------------------------------------------------------------- #
# backtest full flow
# --------------------------------------------------------------------------- #


def test_backtest_full_flow_writes_artifacts(workspace: Workspace) -> None:
    result = backtest(workspace)

    assert result.returncode == 0, result.stderr
    payload = parse_json(result)

    assert payload["mode"] == "backtest"
    assert payload["simulation"] is True
    assert payload["data"]["status"] == "ok"
    assert payload["data"]["codes"][0]["bars"] == 59
    assert payload["metrics"]["trade_count"] > 0
    assert payload["metrics"]["no_lookahead"] is True
    assert payload["metrics"]["t_plus_one"] is True
    assert payload["metrics"]["non_overlapping_windows"] is True
    assert payload["metrics"]["final_equity"] > 0

    for trade in payload["trades"]:
        assert trade["code"] == CODE
        assert trade["shares"] % 100 == 0
        assert trade["shares"] > 0
        assert trade["exit_date"] > trade["entry_date"]
        assert set(trade) == {
            "code",
            "entry_date",
            "entry_price",
            "exit_date",
            "exit_price",
            "shares",
            "fees",
            "pnl",
            "return_pct",
            "reason",
        }
    for row in payload["positions"] + payload["decisions"]:
        assert row["date"] <= DAY

    result_path = Path(payload["artifacts"]["result"])
    sqlite_path = Path(payload["artifacts"]["sqlite"])
    assert result_path.is_file() and sqlite_path.is_file()
    assert payload["artifacts"]["tables"]
    stored = json.loads(result_path.read_text(encoding="utf-8"))
    assert stored["trades"] == payload["trades"]
    assert stored["metrics"] == payload["metrics"]


def test_backtest_costs_reduce_the_simulated_result(workspace: Workspace) -> None:
    plain = parse_json(backtest(workspace))
    costly = parse_json(backtest(workspace, "--slippage", "0.01", "--fee", "0.001"))

    assert costly["metrics"]["final_equity"] < plain["metrics"]["final_equity"]
    assert any(trade["fees"] > 0 for trade in costly["trades"])
    assert all(trade["fees"] == 0 for trade in plain["trades"])
    assert costly["slippage"] == pytest.approx(0.01)
    assert costly["fee"] == pytest.approx(0.001)


def test_backtest_empty_archive_exits_4(workspace: Workspace) -> None:
    empty = Workspace(
        root=workspace.root,
        archive=write_archive(workspace.root / "empty-bt" / "archive", domains=[]),
        config=write_config_dir(
            workspace.root / "empty-bt-config", workspace.root / "empty-bt" / "archive"
        ),
        home=workspace.root / "empty-bt-home",
    )
    result = run_cli(
        "backtest",
        "run",
        "--code",
        CODE,
        "--start",
        START,
        "--end",
        DAY,
        "--out-dir",
        str(workspace.root / "empty-bt" / "out"),
        "--output",
        "json",
        env=empty.env(),
    )

    assert result.returncode == 4, result.stderr
    payload = parse_json(result)
    assert payload["data"]["status"] == "missing"
    assert payload["partial"] is True


def test_backtest_missing_code_reports_partial_exit_8(workspace: Workspace) -> None:
    result = run_cli(
        "backtest",
        "run",
        "--codes",
        f"{CODE},000001",
        "--start",
        START,
        "--end",
        DAY,
        "--out-dir",
        str(workspace.root / "partial"),
        "--output",
        "json",
        env=workspace.env(),
    )

    assert result.returncode == 8, result.stderr
    payload = parse_json(result)
    assert payload["data"]["missing_codes"] == ["000001"]
    assert payload["partial"] is True
    assert payload["per_code"]["000001"]["status"] == "missing"
    assert payload["per_code"][CODE]["status"] == "ok"
    assert not [trade for trade in payload["trades"] if trade["code"] == "000001"]


def test_backtest_replay_is_reproducible_across_out_dirs(workspace: Workspace) -> None:
    first = run_cli(
        "backtest",
        "run",
        "--code",
        CODE,
        "--start",
        START,
        "--end",
        DAY,
        "--out-dir",
        str(workspace.root / "replay-a"),
        "--output",
        "json",
        env=workspace.env(),
    )
    second = run_cli(
        "backtest",
        "run",
        "--code",
        CODE,
        "--start",
        START,
        "--end",
        DAY,
        "--out-dir",
        str(workspace.root / "replay-b"),
        "--output",
        "json",
        env=workspace.env(),
    )

    assert first.returncode == second.returncode == 0
    left, right = parse_json(first), parse_json(second)
    for key in ("decisions", "trades", "positions", "metrics", "per_code", "data"):
        assert left[key] == right[key], key


# --------------------------------------------------------------------------- #
# table / csv / role listing surfaces
# --------------------------------------------------------------------------- #


def test_analyze_csv_writes_one_row_and_no_json_payload(workspace: Workspace) -> None:
    result = run_cli(
        "analyze",
        CODE,
        "--date",
        DAY,
        "--config",
        str(workspace.config),
        "--output",
        "csv",
        env=workspace.env(),
    )

    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.strip().splitlines() if line]
    assert lines[0] == "code,rating,action"
    assert len(lines) == 2
    assert lines[1].startswith(f"{CODE},")


def test_role_list_json_matches_the_configured_roles(workspace: Workspace) -> None:
    result = run_cli(
        "role",
        "list",
        "--config",
        str(CONFIG),
        "--output",
        "json",
        env=workspace.env(),
    )

    assert result.returncode == 0, result.stderr
    rows = parse_json(result)
    assert len(rows) == 16
    assert {"role", "display_name", "enabled", "model"} == set(rows[0])
    assert {row["role"] for row in rows} == {
        "market",
        "social",
        "news",
        "fundamentals",
        "policy",
        "hot_money",
        "lockup",
        "bull",
        "bear",
        "research_manager",
        "trader",
        "risk_aggressive",
        "risk_neutral",
        "risk_conservative",
        "portfolio_manager",
        "quality_gate",
    }
