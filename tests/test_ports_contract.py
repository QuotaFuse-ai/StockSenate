"""Port/topology contract.

The product must not collide with neighbours on the deployment host:
8787 TickHub Admin, 8000 TickHub MCP, 8790/8791 QuotaFuse Wire. StockSenate
owns 8788 (WebUI) and 8765 (MCP HTTP), bound to loopback only.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

import stock_senate.cli as cli

ROOT = Path(__file__).resolve().parents[1]
EXTERNAL_PORTS = {8787, 8000, 8790, 8791}
OWN_PORTS = {8788, 8765}


def help_text(*args: str) -> str:
    result = CliRunner().invoke(cli.app, [*args, "--help"])
    assert result.exit_code == 0
    return re.sub(r"\s+", " ", result.output)


def test_mcp_http_defaults_to_loopback_8765() -> None:
    text = help_text("mcp", "http")

    assert "[default: 127.0.0.1]" in text
    assert "[default: 8765]" in text


def test_webui_defaults_to_loopback_8788() -> None:
    text = help_text("webui")

    assert "[default: 127.0.0.1]" in text
    assert "[default: 8788]" in text


def test_own_ports_are_disjoint_from_neighbours() -> None:
    assert OWN_PORTS.isdisjoint(EXTERNAL_PORTS)


def test_compose_publishes_only_the_owned_ports_on_loopback() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "127.0.0.1:8788:8788" in compose
    assert "127.0.0.1:8765:8765" in compose
    for port in EXTERNAL_PORTS:
        assert f"{port}:{port}" not in compose
    assert "0.0.0.0:8788" not in compose
    assert "0.0.0.0:8765" not in compose


def compose_healthcheck_path() -> str:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    match = re.search(r"http://127\.0\.0\.1:8788([^\s\"']*)", compose)
    assert match is not None, "compose declares no webui healthcheck URL"
    return match.group(1) or "/"


def test_compose_healthcheck_hits_a_route_the_app_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.testclient import TestClient

    from stock_senate.webui.launch import create_app
    from stock_senate.webui.state import WebRuntime

    assert compose_healthcheck_path() == "/healthz"

    monkeypatch.setenv("STOCKSENATE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("STOCKSENATE_WEBUI_PASSWORD", raising=False)
    config = tmp_path / "config"
    shutil.copytree(ROOT / "config", config)

    app = create_app(WebRuntime(config_dir=config))
    with TestClient(app, follow_redirects=False) as client:
        response = client.get(compose_healthcheck_path())

    assert response.status_code == 200
