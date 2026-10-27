"""Public Python API contract.

``propagate`` must keep the ``(final_state, decision)`` shape and the
keyword-only switches the CLI relies on, and it must actually run: the M0
engine is implemented, so the alias and signature are pinned against a real
offline run rather than a placeholder body.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from stock_senate import SenateGraph, TradingAgentsGraph
from stock_senate.report import store
from stock_senate.version import __version__


@pytest.fixture()
def offline_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("STOCKSENATE_HOME", str(home))
    monkeypatch.setenv("STOCKSENATE_LLM_PROVIDER", "offline")
    monkeypatch.setenv("STOCKSENATE_DATA_MODE", "local")
    return home


def test_tradingagents_alias_points_to_senate_graph() -> None:
    assert TradingAgentsGraph is SenateGraph


def test_propagate_signature() -> None:
    signature = inspect.signature(SenateGraph.propagate)
    parameters = signature.parameters
    assert list(parameters)[:3] == ["self", "code", "date"]
    assert parameters["hitl"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["hitl"].default is True
    assert parameters["overrides"].kind is inspect.Parameter.KEYWORD_ONLY


def test_engine_runs_and_persists_the_returned_decision(offline_home: Path) -> None:
    graph = SenateGraph()
    state, decision = graph.propagate("600519", "2026-10-24", hitl=False)

    assert state["run_id"].startswith("2026-10-24-600519-")
    assert state["decision"] == decision
    assert decision["rating"] == state["decision"]["rating"]
    assert decision["execution"] is False
    assert state["report"]["report"].endswith("complete_report.md")

    # The saved snapshot is the same document the caller received.
    saved = store.load(decision["run_id"], home=offline_home)
    assert saved["decision"]["rating"] == decision["rating"]
    assert saved["decision"]["action"] == decision["action"]
    assert saved["decision"]["execution"] is False


def test_version_is_exported() -> None:
    assert __version__ == "1.0.1"
