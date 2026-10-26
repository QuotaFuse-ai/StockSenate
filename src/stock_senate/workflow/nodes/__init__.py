"""Pipeline nodes. Each node is a function over ``RunContext`` + state."""

from stock_senate.workflow.nodes.analysts import run_analysts
from stock_senate.workflow.nodes.base import (
    NodeConfigError,
    RoleOutputError,
    load_system_prompt,
    prompt_root,
    run_role,
)
from stock_senate.workflow.nodes.debate_nodes import run_bull_bear_debate, run_risk_debate
from stock_senate.workflow.nodes.managers import (
    run_portfolio_manager,
    run_research_manager,
    run_trader,
)
from stock_senate.workflow.nodes.quality import run_quality_gate

__all__ = [
    "NodeConfigError",
    "RoleOutputError",
    "load_system_prompt",
    "prompt_root",
    "run_analysts",
    "run_bull_bear_debate",
    "run_portfolio_manager",
    "run_quality_gate",
    "run_research_manager",
    "run_risk_debate",
    "run_role",
    "run_trader",
]
