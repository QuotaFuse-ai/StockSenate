"""One run, three surfaces: CLI, MCP and WebUI must describe the same snapshot.

Traceability row 16.7 asks that the WebUI, CLI and MCP report views agree for
the same ``run_id``. The run is produced once (offline provider + local data
mode), then every surface is read back and compared against the stored state.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from stock_senate.cli import app as cli_app
from stock_senate.graph.senate_graph import SenateGraph
from stock_senate.mcp_server.tools import report_query
from stock_senate.report import store
from stock_senate.report.render import render_summary
from stock_senate.webui.launch import create_app
from stock_senate.webui.state import WebRuntime

ROOT = Path(__file__).resolve().parents[1]
CODE = "600519"
DAY = "2026-10-24"


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    return target


@pytest.fixture()
def state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("STOCKSENATE_HOME", str(home))
    monkeypatch.setenv("STOCKSENATE_LLM_PROVIDER", "offline")
    monkeypatch.setenv("STOCKSENATE_DATA_MODE", "local")
    monkeypatch.delenv("STOCKSENATE_WEBUI_PASSWORD", raising=False)
    monkeypatch.delenv("STOCKSENATE_MCP_HTTP_TOKEN", raising=False)
    return home


@pytest.fixture()
def run_state(config_dir: Path, state_home: Path) -> dict:
    graph = SenateGraph(config=config_dir)
    state, _decision = graph.propagate(CODE, DAY, hitl=False)
    assert state.get("run_id")
    return state


def test_cli_mcp_and_webui_agree_on_one_run(run_state: dict, config_dir: Path) -> None:
    run_id = str(run_state["run_id"])
    rating = str(run_state["decision"]["rating"])

    # MCP: the structured sections come back under the same run id.
    mcp = report_query(run_id=run_id, config=config_dir)
    assert mcp["ok"] is True
    assert mcp["run_id"] == run_id
    assert mcp["summary"] == render_summary(run_state)
    assert mcp["summary"]["code"] == CODE
    assert mcp["summary"]["date"] == DAY
    assert mcp["summary"]["rating"] == rating
    assert mcp["sections"]["decision"]["rating"] == rating

    # CLI: `report show --output json` is the persisted snapshot, not a re-run.
    result = CliRunner().invoke(cli_app, ["report", "show", run_id, "--output", "json"])
    assert result.exit_code == 0, result.output
    snapshot = json.loads(result.output)
    assert snapshot["run_id"] == run_id
    assert snapshot["code"] == CODE
    assert snapshot["decision"]["rating"] == rating

    # WebUI: detail page, JSON API and Markdown download all serve that run.
    runtime = WebRuntime(config_dir=config_dir)
    app = create_app(runtime)
    with TestClient(app, base_url="http://127.0.0.1", follow_redirects=False) as client:
        detail = client.get(f"/reports/{run_id}")
        api = client.get(f"/reports/{run_id}/json")
        markdown = client.get(f"/reports/{run_id}/markdown")

    assert detail.status_code == 200
    assert run_id in detail.text
    assert CODE in detail.text
    assert rating.lower() in detail.text.lower()

    assert api.status_code == 200
    assert api.json()["state"]["run_id"] == run_id
    assert api.json()["state"]["decision"]["rating"] == rating

    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert markdown.text == store.render(run_id)
    assert f"`{run_id}`" in markdown.text
    assert markdown.text == store.render(run_id, home=runtime.home)
