"""Run-scoped state shared by every workflow node."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stock_senate.data.envelope import DataEnvelope
from stock_senate.data.router import DataAccessLayer
from stock_senate.roles.llm import RoleLLMResolver
from stock_senate.roles.registry import RoleRegistry
from stock_senate.roles.tool_gate import ToolGate
from stock_senate.workflow.budget import Budget

HitlHandler = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]


@dataclass
class RunContext:
    code: str
    date: str
    config: dict[str, Any]
    registry: RoleRegistry
    gate: ToolGate
    resolver: RoleLLMResolver
    data: DataAccessLayer
    envelopes: dict[str, DataEnvelope]
    digest: dict[str, Any]
    budget: Budget
    trace_id: str
    home: Path
    hitl_handler: HitlHandler | None = None
    budget_tripped: str = ""
    skipped: list[dict[str, str]] = field(default_factory=list)
    usage: list[dict[str, Any]] = field(default_factory=list)

    @property
    def weights(self) -> dict[str, float]:
        return self.registry.weights

    def envelope(self, domain: str) -> DataEnvelope | None:
        return self.envelopes.get(domain)

    def role_domains(self, role: str) -> list[str]:
        return self.gate.domains(role)

    def analyst_roles(self) -> list[str]:
        return [spec.key for spec in self.registry.enabled_analysts()]

    def data_summary(self) -> dict[str, Any]:
        from stock_senate.data.router import summarize

        return summarize(self.envelopes)


__all__ = ["HitlHandler", "RunContext"]
