"""Public graph API.

``SenateGraph.propagate(code, date)`` is the stable contract: it must keep
returning ``(final_state, decision)`` exactly like the upstream
``TradingAgentsGraph.propagate``. Report files are derived outputs, never a
replacement for that return value.

The implementation lands with milestone M0. This module defines the contract
and argument surface so downstream code and tests can be written against it.
"""

from __future__ import annotations

from typing import Any

from stock_senate.version import __version__ as _package_version


class SenateGraph:
    """Multi-role research graph.

    Parameters
    ----------
    debug:
        Emit verbose node-level logs to stderr.
    config:
        Optional overrides merged over ``config/llm_config.toml`` and the
        built-in defaults. Unknown keys raise rather than being ignored.
    """

    def __init__(self, debug: bool = False, config: dict[str, Any] | None = None) -> None:
        self.debug = debug
        self.config: dict[str, Any] = dict(config or {})

    def propagate(
        self,
        code: str,
        date: str,
        *,
        hitl: bool = True,
        overrides: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run the full graph for ``code`` as of ``date``.

        Returns ``(final_state, decision)``. ``decision`` carries at least
        ``rating`` (Buy/Hold/Sell), ``reason`` and the A-share constraint flags.
        ``hitl=False`` skips the human checkpoints after the trader and before
        the final verdict; ``overrides`` carries the per-run role
        enable/disable/weight flags from the CLI. Both are keyword-only so the
        upstream positional signature keeps working.
        """
        raise NotImplementedError(
            "SenateGraph.propagate is implemented in milestone M0; "
            f"this is the {_package_version} contract scaffold."
        )
