"""Checkpoints and resume: an interrupted run must not pay twice.

The contract these tests pin:

* checkpoints are off unless the configuration turns them on, so a default
  install never writes state to disk;
* with them on, a run that dies mid-pipeline leaves the completed stages
  behind and the retry restores them without calling the node again;
* the resumed result is identical to an uninterrupted run of the same code,
  date and configuration;
* a finished run clears its rows, ``resume=False`` replays everything, and a
  changed HITL mode never reuses a trace recorded under the other mode;
* a configuration failure drops its rows (the fixed configuration changes the
  key, so they are unreachable), while a rate limit keeps them for the retry.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import stock_senate.graph.senate_graph as graph_module
from stock_senate import SenateGraph
from stock_senate.config.loader import load_config
from stock_senate.roles.llm import LLMRateLimited
from stock_senate.workflow.checkpoint import CheckpointStore, store_from_config
from stock_senate.workflow.nodes.base import NodeConfigError

CODE = "600519"
DAY = "2026-10-24"

NODE_NAMES = (
    "run_analysts",
    "run_bull_bear_debate",
    "run_research_manager",
    "run_trader",
    "run_risk_debate",
    "run_portfolio_manager",
    "run_quality_gate",
)

# Fields of the returned decision that do not depend on the run's identity or
# on when it happened; two identical analyses must agree on all of them.
_STABLE_DECISION_KEYS = (
    "rating",
    "action",
    "execution",
    "horizon",
    "confidence",
    "reasons",
    "risks",
    "partial",
    "low_quality",
    "quality_score",
    "quality_threshold",
    "budget_tripped",
    "skipped",
)


@pytest.fixture()
def offline_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("STOCKSENATE_HOME", str(home))
    monkeypatch.setenv("STOCKSENATE_LLM_PROVIDER", "offline")
    monkeypatch.setenv("STOCKSENATE_DATA_MODE", "local")
    return home


@pytest.fixture()
def nodes(monkeypatch: pytest.MonkeyPatch) -> tuple[Counter[str], dict[str, str | None]]:
    """Count node calls and let a test inject an interrupt at one stage."""
    counts: Counter[str] = Counter()
    fail: dict[str, str | None] = {"at": None}
    for name in NODE_NAMES:
        real = getattr(graph_module, name)

        def wrapper(*args: Any, __real: Any = real, __name: str = name, **kwargs: Any) -> Any:
            counts[__name] += 1
            if fail["at"] == __name:
                raise RuntimeError(f"simulated interrupt at {__name}")
            return __real(*args, **kwargs)

        monkeypatch.setattr(graph_module, name, wrapper)
    return counts, fail


def _checkpoint_config(tmp_path: Path) -> dict[str, Any]:
    return {
        "workflow": {
            "checkpoint": {
                "enabled": True,
                "backend": "sqlite",
                "path": str(tmp_path / "checkpoints.sqlite"),
            }
        }
    }


def _projection(state: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision": {key: decision[key] for key in _STABLE_DECISION_KEYS},
        "analysts": state["analysts"],
        "debate": state["debate"],
        "research": state["research"],
        "trade": state["trade"],
        "risk": state["risk"],
        "quality": state["quality"],
    }


def test_checkpoints_are_off_by_default(offline_home: Path) -> None:
    config = load_config()
    assert config["workflow"]["checkpoint"]["enabled"] is False
    assert store_from_config(config) is None

    state, _ = SenateGraph().propagate(CODE, DAY, hitl=False)
    assert "checkpoint" not in state
    assert not list(offline_home.glob("*.sqlite"))


def test_resume_skips_completed_nodes_and_matches_an_uninterrupted_run(
    offline_home: Path,
    nodes: tuple[Counter[str], dict[str, str | None]],
    tmp_path: Path,
) -> None:
    counts, fail = nodes
    reference_state, reference_decision = SenateGraph().propagate(CODE, DAY, hitl=False)
    assert "checkpoint" not in reference_state

    config = _checkpoint_config(tmp_path)
    graph = SenateGraph(config=config)
    store = CheckpointStore(tmp_path / "checkpoints.sqlite")
    config_hash = str(graph.config["config_hash"])

    fail["at"] = "run_trader"
    with pytest.raises(RuntimeError, match="simulated interrupt"):
        graph.propagate(CODE, DAY, hitl=False)

    pending = store.load(code=CODE, date=DAY, config_hash=config_hash)
    assert set(pending.stages) >= {"analysts", "debate", "research", "_run_state"}
    assert "trader" not in pending.stages

    counts.clear()
    fail["at"] = None
    progress: list[tuple[str, str, dict[str, Any]]] = []
    state, decision = graph.propagate(
        CODE, DAY, hitl=False, progress=lambda *event: progress.append(event)
    )

    assert counts["run_analysts"] == 0
    assert counts["run_bull_bear_debate"] == 0
    assert counts["run_research_manager"] == 0
    assert counts["run_trader"] == 1
    assert state["checkpoint"] == {
        "enabled": True,
        "resumed": ["analysts", "debate", "research"],
    }
    assert any(fields.get("restored") for _, _, fields in progress)
    assert _projection(state, decision) == _projection(reference_state, reference_decision)

    # A finished run must never be served from a checkpoint again.
    assert not store.load(code=CODE, date=DAY, config_hash=config_hash)


def test_resume_false_replays_every_stage(
    offline_home: Path,
    nodes: tuple[Counter[str], dict[str, str | None]],
    tmp_path: Path,
) -> None:
    counts, fail = nodes
    graph = SenateGraph(config=_checkpoint_config(tmp_path))

    fail["at"] = "run_trader"
    with pytest.raises(RuntimeError, match="simulated interrupt"):
        graph.propagate(CODE, DAY, hitl=False)

    counts.clear()
    fail["at"] = None
    state, _ = graph.propagate(CODE, DAY, hitl=False, resume=False)

    assert counts["run_analysts"] == 1
    assert counts["run_research_manager"] == 1
    assert state["checkpoint"] == {"enabled": True, "resumed": []}


def test_a_changed_hitl_mode_does_not_reuse_another_modes_trace(
    offline_home: Path,
    nodes: tuple[Counter[str], dict[str, str | None]],
    tmp_path: Path,
) -> None:
    counts, fail = nodes
    graph = SenateGraph(config=_checkpoint_config(tmp_path))

    fail["at"] = "run_risk_debate"
    with pytest.raises(RuntimeError, match="simulated interrupt"):
        graph.propagate(CODE, DAY, hitl=True)

    counts.clear()
    fail["at"] = None
    state, _ = graph.propagate(CODE, DAY, hitl=False)

    traces = state["hitl"]["traces"]
    assert [trace["enabled"] for trace in traces] == [False, False]
    assert [trace["decision"] for trace in traces] == ["skipped", "skipped"]
    assert state["checkpoint"] == {
        "enabled": True,
        "resumed": ["analysts", "debate", "research", "trader"],
    }


def test_a_configuration_error_discards_the_checkpoint(
    offline_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    graph = SenateGraph(config=_checkpoint_config(tmp_path))
    store = CheckpointStore(tmp_path / "checkpoints.sqlite")
    config_hash = str(graph.config["config_hash"])

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise NodeConfigError("reviewer role has no model")

    monkeypatch.setattr(graph_module, "run_trader", broken)
    with pytest.raises(NodeConfigError, match="no model"):
        graph.propagate(CODE, DAY, hitl=False)

    # Earlier stages ran and saved rows, but the fixed configuration will
    # change ``config_hash``, so those rows could never be resumed again.
    assert not store.load(code=CODE, date=DAY, config_hash=config_hash)


def test_a_rate_limited_run_keeps_its_checkpoint_for_the_retry(
    offline_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    graph = SenateGraph(config=_checkpoint_config(tmp_path))
    store = CheckpointStore(tmp_path / "checkpoints.sqlite")
    config_hash = str(graph.config["config_hash"])

    def limited(*args: Any, **kwargs: Any) -> Any:
        raise LLMRateLimited("rate limited", retry_after=2.0)

    monkeypatch.setattr(graph_module, "run_trader", limited)
    with pytest.raises(LLMRateLimited):
        graph.propagate(CODE, DAY, hitl=False)

    # The retry runs under the same key, so the completed stages stay.
    pending = store.load(code=CODE, date=DAY, config_hash=config_hash)
    assert set(pending.stages) >= {"analysts", "debate", "research", "_run_state"}


def test_store_roundtrip_and_a_corrupt_row_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.sqlite"
    store = CheckpointStore(path)
    store.save(
        code=CODE,
        date=DAY,
        config_hash="h",
        stage="analysts",
        payload={"reports": []},
        budget={"calls": 3},
    )

    state = store.load(code=CODE, date=DAY, config_hash="h")
    assert state.stages == {"analysts": {"reports": []}}
    assert state.budget == {"calls": 3}
    assert state.names == ["analysts"]

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT OR REPLACE INTO checkpoints (run_key, stage, payload, budget, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (f"{CODE}|{DAY}|h", "debate", "{not json", "{}", "2026-10-24T00:00:00+00:00"),
        )

    state = store.load(code=CODE, date=DAY, config_hash="h")
    assert state.names == ["analysts"]

    store.clear(code=CODE, date=DAY, config_hash="h")
    assert not store.load(code=CODE, date=DAY, config_hash="h")
