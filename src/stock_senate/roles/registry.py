"""Role registry: canonical keys, aliases, enable/disable and seat limits.

``agent_roles.toml`` is the single source of truth for which roles exist. The
registry normalises aliases, applies temporary CLI overrides (``--enable``,
``--disable``, ``--weight``) without touching disk, and enforces the seat cap
defined by DEC-005: at most twelve analyst seats, seven built-in plus five
custom. Researchers, the trader, the risk debaters and the managers are fixed
stages and do not consume a seat.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from stock_senate.config.schema import BUILTIN_ANALYSTS, ROLE_ALIASES, ROLE_LLM_KEYS


class RoleError(ValueError):
    """An unknown role, a duplicate alias or a broken seat limit."""


@dataclass(frozen=True)
class RoleSpec:
    key: str
    display_name: str
    kind: str
    prompt: str
    tools: tuple[str, ...]
    output_schema: str
    enabled: bool = True
    aliases: tuple[str, ...] = ()
    custom: bool = False
    weight: float = 1.0

    @property
    def is_analyst(self) -> bool:
        return self.kind == "analyst"

    def as_row(self) -> dict[str, Any]:
        return {
            "role": self.key,
            "display_name": self.display_name,
            "kind": self.kind,
            "enabled": self.enabled,
            "custom": self.custom,
            "output_schema": self.output_schema,
            "weight": self.weight,
        }


def _custom_table(section: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    """Custom roles may live under ``[custom]`` or ``[custom.roles]``."""
    if not section:
        return {}
    if isinstance(section.get("roles"), Mapping):
        return dict(section["roles"])
    return {
        str(key): value
        for key, value in section.items()
        if isinstance(value, Mapping) and "output_schema" in value
    }


class RoleRegistry:
    """Immutable view over the configured roles."""

    def __init__(
        self,
        roles: Mapping[str, RoleSpec],
        *,
        aliases: Mapping[str, str] | None = None,
        max_roles: int = 12,
        max_custom_roles: int = 5,
        enforce_tools: bool = True,
    ) -> None:
        self.roles: dict[str, RoleSpec] = dict(roles)
        self.alias_map: dict[str, str] = dict(ROLE_ALIASES)
        self.alias_map.update(aliases or {})
        self.max_roles = int(max_roles)
        self.max_custom_roles = int(max_custom_roles)
        self.enforce_tools = enforce_tools
        self._validate_aliases()
        self._validate_seats()

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> RoleRegistry:
        section = (config or {}).get("agent_roles", {}) if config else {}
        raw = dict(section.get("roles") or {})
        for key, value in _custom_table(section.get("custom")).items():
            raw.setdefault(key, value)

        specs: dict[str, RoleSpec] = {}
        aliases: dict[str, str] = {}
        for key, value in raw.items():
            value = value or {}
            if not isinstance(value, Mapping):
                raise RoleError(f"roles.{key} 必须是表")
            kind = str(value.get("kind") or "").strip()
            if not kind:
                raise RoleError(f"roles.{key}.kind 必填")
            schema = str(value.get("output_schema") or "").strip()
            if not schema:
                raise RoleError(f"roles.{key}.output_schema 必填")
            declared = tuple(str(item) for item in (value.get("alias") or ()))
            key = str(key)
            custom = key not in ROLE_LLM_KEYS
            specs[key] = RoleSpec(
                key=key,
                display_name=str(value.get("display_name") or key),
                kind=kind,
                prompt=str(value.get("prompt") or ""),
                tools=tuple(str(item) for item in (value.get("tools") or ())),
                output_schema=schema,
                enabled=bool(value.get("enabled", True)),
                aliases=declared,
                custom=custom,
            )
            for alias in declared:
                target = aliases.get(alias)
                if target is not None and target != key:
                    raise RoleError(f"别名 {alias!r} 同时指向 {target} 与 {key}")
                aliases[alias] = key

        limits = section.get("limits") or {}
        tools_cfg = section.get("tool_permissions") or {}
        registry = cls(
            specs,
            aliases=aliases,
            max_roles=int(limits.get("max_roles", 12)),
            max_custom_roles=int(limits.get("max_custom_roles", 5)),
            enforce_tools=tools_cfg.get("enforce", True) is not False,
        )
        if tools_cfg.get("deny_write_tools", True) is not True:
            raise RoleError("tool_permissions.deny_write_tools 必须为 true：本系统不做实盘交易")
        return registry

    def _validate_aliases(self) -> None:
        for alias, target in self.alias_map.items():
            if target not in self.roles:
                # A built-in alias for a removed role is harmless; drop it.
                continue
            if alias in self.roles and alias != target:
                raise RoleError(f"别名 {alias!r} 与现有角色键冲突")
            for key, spec in self.roles.items():
                if alias in spec.aliases and key != target:
                    raise RoleError(f"别名 {alias!r} 同时指向 {key} 与 {target}")

    def _validate_seats(self) -> None:
        enabled_analysts = [
            spec for spec in self.roles.values() if spec.enabled and spec.is_analyst
        ]
        if len(enabled_analysts) > self.max_roles:
            raise RoleError(
                f"启用 {len(enabled_analysts)} 个分析师席位，超过上限 {self.max_roles}"
                f"（{len(BUILTIN_ANALYSTS)} 内置 + {self.max_custom_roles} 自定义）"
            )
        custom = [spec for spec in enabled_analysts if spec.custom]
        if len(custom) > self.max_custom_roles:
            raise RoleError(f"启用 {len(custom)} 个自定义分析师，超过上限 {self.max_custom_roles}")

    # ------------------------------------------------------------------ #
    # lookup
    # ------------------------------------------------------------------ #

    def canonical(self, name: str) -> str:
        key = str(name or "").strip()
        if key in self.roles:
            return key
        target = self.alias_map.get(key)
        if target and target in self.roles:
            return target
        raise RoleError(f"未知角色: {name}；可用角色 {', '.join(sorted(self.roles))}")

    def get(self, name: str) -> RoleSpec:
        return self.roles[self.canonical(name)]

    def all(self) -> list[RoleSpec]:
        return [self.roles[key] for key in sorted(self.roles)]

    def analysts(self, *, enabled_only: bool = False) -> list[RoleSpec]:
        return [
            spec for spec in self.all() if spec.is_analyst and (spec.enabled or not enabled_only)
        ]

    def enabled_analysts(self) -> list[RoleSpec]:
        return self.analysts(enabled_only=True)

    def with_overrides(
        self,
        *,
        enable: Iterable[str] | None = None,
        disable: Iterable[str] | None = None,
        weights: Iterable[str] | Mapping[str, Any] | None = None,
    ) -> RoleRegistry:
        """Return a copy with temporary CLI overrides applied."""
        roles = dict(self.roles)
        for name in enable or ():
            key = self.canonical(name)
            roles[key] = replace(roles[key], enabled=True)
        for name in disable or ():
            key = self.canonical(name)
            roles[key] = replace(roles[key], enabled=False)
        parsed = _parse_weights(weights)
        for name, value in parsed.items():
            key = self.canonical(name)
            roles[key] = replace(roles[key], weight=value)
        return RoleRegistry(
            roles,
            aliases=self.alias_map,
            max_roles=self.max_roles,
            max_custom_roles=self.max_custom_roles,
            enforce_tools=self.enforce_tools,
        )

    @property
    def weights(self) -> dict[str, float]:
        return {spec.key: spec.weight for spec in self.roles.values()}

    def summary(self) -> list[dict[str, Any]]:
        return [spec.as_row() for spec in self.all()]


def _parse_weights(weights: Iterable[str] | Mapping[str, Any] | None) -> dict[str, float]:
    if not weights:
        return {}
    if isinstance(weights, Mapping):
        items = weights.items()
    else:
        parsed: dict[str, float] = {}
        for item in weights:
            text = str(item)
            if "=" not in text:
                raise RoleError(f"--weight 需要形如 role=0.6，收到 {text!r}")
            key, _, value = text.partition("=")
            parsed[key.strip()] = _positive(value, key)
        return parsed
    return {str(key): _positive(value, str(key)) for key, value in items}


def _positive(value: Any, key: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RoleError(f"角色 {key} 的权重不是数字: {value!r}") from exc
    if number <= 0:
        raise RoleError(f"角色 {key} 的权重必须大于 0")
    return number


__all__ = ["RoleError", "RoleRegistry", "RoleSpec"]
