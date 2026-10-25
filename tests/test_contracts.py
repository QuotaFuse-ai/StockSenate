"""Public Python API contract.

``propagate`` must keep the upstream ``(final_state, decision)`` shape and the
keyword-only switches the CLI relies on. The engine itself lands in M0, so the
body raising ``NotImplementedError`` is part of the current contract, not an
accident — the alias and signature must not drift while it is being built.
"""

from __future__ import annotations

import inspect

import pytest

from stock_senate import SenateGraph, TradingAgentsGraph
from stock_senate.version import __version__


def test_tradingagents_alias_points_to_senate_graph() -> None:
    assert TradingAgentsGraph is SenateGraph


def test_propagate_signature() -> None:
    signature = inspect.signature(SenateGraph.propagate)
    parameters = signature.parameters
    assert list(parameters)[:3] == ["self", "code", "date"]
    assert parameters["hitl"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["hitl"].default is True
    assert parameters["overrides"].kind is inspect.Parameter.KEYWORD_ONLY


def test_engine_is_not_implemented_yet() -> None:
    graph = SenateGraph()
    with pytest.raises(NotImplementedError):
        graph.propagate("600519", "2026-10-24")


def test_version_is_exported() -> None:
    assert __version__ == "0.1.0"
