"""The CLI reads ``./.env`` once, without overriding exported variables."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
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

    process = subprocess.Popen(
        [sys.executable, "-c", "from stock_senate.cli import main; main()", "mcp", "stdio"],
        cwd=tmp_path,
        env=_cli_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert process.stdin is not None and process.stdout is not None
    assert process.stderr is not None
    process.stdin.write(stdin)
    process.stdin.flush()

    # Closing stdin immediately makes the stdio server see EOF while tools/list
    # is still being dispatched, and it can shut down before writing that
    # response. Read both frames first, then close stdin to stop the server.
    lines: list[str] = []
    threading.Thread(
        target=lambda: lines.extend(raw.strip() for raw in process.stdout if raw.strip()),
        daemon=True,
    ).start()
    deadline = time.monotonic() + 60
    while len(lines) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)

    process.stdin.close()
    stderr = process.stderr.read()
    process.wait(timeout=90)

    assert process.returncode == 0, stderr[-1500:]
    assert len(lines) == 2
    for line in lines:
        message = json.loads(line)
        assert message["jsonrpc"] == "2.0"
    assert SENTINEL not in "\n".join(lines)
    assert SENTINEL not in stderr
