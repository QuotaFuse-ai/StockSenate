"""Tool whitelist enforcement at run time.

The config validator checks the file; this gate is the code-level boundary that
decides which data domains a role may read. A role sees only the domains named
by its own ``tools`` list, and write tools (orders, config edits) are never
granted to any role.
"""

from __future__ import annotations

from typing import Iterable

from stock_senate.data.providers.base import TOOL_TO_DOMAIN, tool_to_domain
from stock_senate.roles.registry import RoleRegistry, RoleSpec

WRITE_TOOLS = frozenset(
    {
        "place_order",
        "submit_order",
        "cancel_order",
        "modify_order",
        "trade",
        "buy",
        "sell",
        "transfer",
        "withdraw",
        "write_file",
        "edit_config",
        "set_config",
        "delete_file",
        "shell",
        "exec",
    }
)


class ToolDenied(PermissionError):
    """Raised when a role asks for a tool or domain it does not own."""


class ToolGate:
    def __init__(self, registry: RoleRegistry) -> None:
        self.registry = registry
        self.enforce = registry.enforce_tools

    def allowed_tools(self, role: str) -> tuple[str, ...]:
        return self.registry.get(role).tools

    def domains(self, role: str) -> list[str]:
        seen: list[str] = []
        for tool in self.allowed_tools(role):
            domain = tool_to_domain(tool)
            if domain not in seen:
                seen.append(domain)
        return seen

    def domains_for(self, roles: Iterable[str]) -> list[str]:
        seen: list[str] = []
        for role in roles:
            for domain in self.domains(role):
                if domain not in seen:
                    seen.append(domain)
        return seen

    def check(self, role: str, tool: str) -> str:
        """Return the data domain for ``tool`` or raise ``ToolDenied``."""
        name = str(tool)
        if name in WRITE_TOOLS:
            raise ToolDenied(f"角色 {role} 请求写工具 {name!r}：本系统不提供任何写/交易工具")
        spec: RoleSpec = self.registry.get(role)
        if not self.enforce:
            known = name in TOOL_TO_DOMAIN
            if not known:
                raise ToolDenied(f"未知数据工具 {name!r}")
            return tool_to_domain(name)
        if name not in spec.tools:
            raise ToolDenied(
                f"角色 {role} 未获得工具 {name!r}；白名单为 {list(spec.tools)}"
            )
        return tool_to_domain(name)

    def check_domain(self, role: str, domain: str) -> None:
        allowed = self.domains(role)
        if domain not in allowed:
            raise ToolDenied(
                f"角色 {role} 不得读取 {domain!r}；允许的域为 {allowed}"
            )


__all__ = ["ToolDenied", "ToolGate", "WRITE_TOOLS"]
