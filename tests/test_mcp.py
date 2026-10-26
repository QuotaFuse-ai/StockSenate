"""MCP surface: tool registry, credential masking, HTTP auth, stdio purity.

The HTTP transport is exercised at the ASGI seam (no socket, no client extra);
stdio is checked end-to-end because "stdout carries only JSON-RPC" is a
security boundary, not an implementation detail.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from stock_senate.mcp_server import server, tools

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

EXPECTED_TOOLS = {
    "stock_senate_analysis",
    "stock_senate_backtest",
    "stock_senate_alert_query",
    "stock_senate_report_query",
    "stock_senate_config_query",
}


def test_registry_exposes_five_prefixed_tools() -> None:
    async def run() -> list[str]:
        instance = server.build()
        return [tool.name for tool in await instance.list_tools()]

    names = asyncio.run(run())
    assert set(names) == EXPECTED_TOOLS
    assert len(names) == 5


def test_service_identity_is_stock_senate() -> None:
    assert server.SERVICE_NAME == "stock-senate"
    assert server.DEFAULT_HOST == "127.0.0.1"
    assert server.DEFAULT_PORT == 8765


def test_config_query_masks_values_but_keeps_counts() -> None:
    config = {
        "llm_config": {
            "llm": {
                "api_key": "sk-live-should-never-appear",
                "access_token": "tok-should-never-appear",
                "max_tokens": 8192,
            }
        }
    }
    result = tools.config_query("llm_config", config=config)
    llm = result["config"]["llm_config"]["llm"]
    assert llm["api_key"] == "***"
    assert llm["access_token"] == "***"
    assert llm["max_tokens"] == 8192
    dumped = json.dumps(result, ensure_ascii=False)
    assert "sk-live-should-never-appear" not in dumped
    assert "tok-should-never-appear" not in dumped


def test_config_query_rejects_unknown_section() -> None:
    result = tools.config_query("nope")
    assert result["ok"] is False
    assert result["error"]["kind"] == "invalid_request"
    assert "llm_config" in result["error"]["available"]


def test_alert_query_reads_local_log(tmp_path: Path) -> None:
    from stock_senate.alerts.channels import webui

    webui.send(
        {"id": "a1", "code": "600519", "level": "info", "as_of": "2026-10-20"},
        {"channels": {}},
        home=tmp_path,
    )
    webui.send(
        {"id": "a2", "code": "000001", "level": "warning", "as_of": "2026-10-21"},
        {"channels": {}},
        home=tmp_path,
    )
    result = tools.alert_query("600519", home=tmp_path)
    assert result["ok"] is True
    assert [row["id"] for row in result["alerts"]] == ["a1"]


def test_loopback_does_not_need_a_token() -> None:
    for host in ("127.0.0.1", "::1", "localhost", "[::1]"):
        assert server.is_loopback(host), host
        assert server.require_token_for(host, token="") == ""


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "example.com", ""])
def test_non_loopback_requires_a_token(host: str) -> None:
    assert not server.is_loopback(host)
    with pytest.raises(server.MCPAuthError):
        server.require_token_for(host, token="")
    assert server.require_token_for(host, token="abc") == "abc"


class _Downstream:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope, receive, send) -> None:
        self.calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _probe(middleware, headers: list[tuple[bytes, bytes]], path: str = "/mcp") -> tuple[int, bytes]:
    events: list[dict] = []

    async def send(message: dict) -> None:
        events.append(message)

    async def receive() -> dict:
        return {"type": "http.request", "body": b""}

    asyncio.run(
        middleware({"type": "http", "path": path, "headers": headers}, receive, send)
    )
    status = events[0]["status"]
    body = events[-1].get("body", b"")
    return status, body


def test_bearer_auth_rejects_missing_and_wrong_tokens() -> None:
    downstream = _Downstream()
    middleware = server.BearerAuthMiddleware(downstream, "s3cret")

    status, _ = _probe(middleware, [])
    assert status == 401
    status, _ = _probe(middleware, [(b"authorization", b"Bearer nope")])
    assert status == 401
    status, _ = _probe(middleware, [(b"authorization", b"Bearer s3cret")])
    assert status == 200
    assert downstream.calls == 1


def test_health_stays_open_without_a_token() -> None:
    downstream = _Downstream()
    middleware = server.BearerAuthMiddleware(downstream, "s3cret")
    status, _ = _probe(middleware, [], path=server.HEALTH_PATH)
    assert status == 200
    assert downstream.calls == 1


def _stdio_session(payloads: list[dict], timeout: float = 120.0) -> list[dict]:
    """Drive a real stdio subprocess: send, wait for each reply, then close.

    Writing every payload in one batch and closing stdin immediately races the
    server's shutdown: under load the EOF can cancel a request whose response
    is still being produced. A client waits for its reply before closing, so
    the session does the same.
    """
    env = dict(os.environ)
    env.update(
        PYTHONIOENCODING="utf-8",
        PYTHONPATH=str(SRC),
        STOCKSENATE_MCP_TOKEN="",
    )
    script = "from stock_senate.cli import main; main()"
    messages: list[dict] = []
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
        proc = subprocess.Popen(
            [sys.executable, "-c", script, "mcp", "stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            encoding="utf-8",
            env=env,
        )
        replies: queue.Queue[str | None] = queue.Queue()

        def _pump() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                replies.put(line)
            replies.put(None)

        threading.Thread(target=_pump, daemon=True).start()
        deadline = time.monotonic() + timeout
        try:
            for payload in payloads:
                assert proc.stdin is not None
                proc.stdin.write(json.dumps(payload) + "\n")
                proc.stdin.flush()
                if "id" not in payload:
                    continue
                remaining = deadline - time.monotonic()
                try:
                    line = replies.get(timeout=max(1.0, remaining))
                except queue.Empty:
                    raise AssertionError(f"no reply for {payload!r}") from None
                assert line is not None, f"server closed before replying to {payload!r}"
                messages.append(json.loads(line))
        finally:
            if proc.stdin is not None:
                proc.stdin.close()
            proc.wait(timeout=timeout)
        stderr_file.seek(0)
        stderr = stderr_file.read()

    assert proc.returncode == 0, stderr[-1500:]
    assert messages, stderr[-1500:]
    for message in messages:
        assert message["jsonrpc"] == "2.0"
    return messages


def test_stdio_stdout_is_only_jsonrpc() -> None:
    messages = _stdio_session(
        [
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
    )
    init = next(item for item in messages if item.get("id") == 1)
    assert init["result"]["serverInfo"]["name"] == "stock-senate"
    listed = next(item for item in messages if item.get("id") == 2)
    assert {tool["name"] for tool in listed["result"]["tools"]} == EXPECTED_TOOLS
