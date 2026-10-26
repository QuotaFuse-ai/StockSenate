"""The CLI reads ``./.env`` once, without overriding exported variables."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from stock_senate import cli
from stock_senate.roles.llm import RoleLLMResolver
from tests.cli_support import SRC

SENTINEL = "sk-audit-dotenv-0000"


def _cli_env(**overrides: str) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(overrides)
    return env


def test_dotenv_value_reaches_the_llm_resolver(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STOCKSENATE_LLM_API_KEY", raising=False)
    (tmp_path / ".env").write_text(f"STOCKSENATE_LLM_API_KEY={SENTINEL}\n", encoding="utf-8")

    cli._load_dotenv()

    assert os.environ["STOCKSENATE_LLM_API_KEY"] == SENTINEL
    resolver = RoleLLMResolver({"llm_config": {"llm": {"llm_provider": "minimax"}}})
    assert resolver._api_key("minimax") == SENTINEL


def test_exported_environment_wins_over_dotenv(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STOCKSENATE_LLM_API_KEY", "from-export")
    (tmp_path / ".env").write_text(f"STOCKSENATE_LLM_API_KEY={SENTINEL}\n", encoding="utf-8")

    cli._load_dotenv()

    assert os.environ["STOCKSENATE_LLM_API_KEY"] == "from-export"


def test_cli_reads_config_dir_from_dotenv(tmp_path: Path) -> None:
    config_dir = tmp_path / "from-dotenv"
    config_dir.mkdir()
    (tmp_path / ".env").write_text(
        f"STOCKSENATE_CONFIG_DIR='{config_dir.as_posix()}'\n", encoding="utf-8"
    )
    env = _cli_env()
    env.pop("STOCKSENATE_CONFIG_DIR", None)

    result = subprocess.run(
        [sys.executable, "-m", "stock_senate", "config", "path"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(config_dir)


def test_cli_exported_config_dir_beats_dotenv(tmp_path: Path) -> None:
    exported = tmp_path / "exported"
    exported.mkdir()
    (tmp_path / ".env").write_text(
        f"STOCKSENATE_CONFIG_DIR='{(tmp_path / 'ignored').as_posix()}'\n", encoding="utf-8"
    )

    result = subprocess.run(
        [sys.executable, "-m", "stock_senate", "config", "path"],
        cwd=tmp_path,
        env=_cli_env(STOCKSENATE_CONFIG_DIR=str(exported)),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(exported)


def test_mcp_stdio_stdout_is_still_only_jsonrpc_with_a_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        f"STOCKSENATE_DATA_MODE=local\nSTOCKSENATE_LLM_API_KEY={SENTINEL}\n",
        encoding="utf-8",
    )
    payloads = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    stdin = "".join(json.dumps(item) + "\n" for item in payloads)

    result = subprocess.run(
        [sys.executable, "-c", "from stock_senate.cli import main; main()", "mcp", "stdio"],
        cwd=tmp_path,
        env=_cli_env(),
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )

    assert result.returncode == 0, result.stderr[-1500:]
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 2
    for line in lines:
        message = json.loads(line)
        assert message["jsonrpc"] == "2.0"
    assert SENTINEL not in result.stdout
    assert SENTINEL not in result.stderr
