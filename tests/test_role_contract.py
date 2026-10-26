"""Role contract: canonical keys, aliases, seat caps and CLI overrides."""

from __future__ import annotations

import pytest

from cli_support import CONFIG, run_cli
from stock_senate.config.loader import load_config
from stock_senate.config.schema import ROLE_ALIASES, ROLE_LLM_KEYS
from stock_senate.roles.registry import RoleError, RoleRegistry

ANALYSTS = ("market", "social", "news", "fundamentals", "policy", "hot_money", "lockup")


def role(**extra):
    base = {
        "display_name": "角色",
        "kind": "analyst",
        "prompt": "prompts/role.md",
        "tools": [],
        "output_schema": "AnalystReport",
        "enabled": True,
    }
    base.update(extra)
    return base


def config_for(roles: dict, *, limits: dict | None = None, tools: dict | None = None) -> dict:
    return {
        "agent_roles": {
            "roles": roles,
            "limits": limits or {},
            "tool_permissions": tools or {"deny_write_tools": True},
        }
    }


def registry_for(roles: dict, **kwargs) -> RoleRegistry:
    return RoleRegistry.from_config(config_for(roles, **kwargs))


def builtin_roles() -> dict:
    return {key: role() for key in ANALYSTS}


def custom_roles(count: int) -> dict:
    return {f"custom_{index}": role(display_name=f"自定义{index}") for index in range(count)}


@pytest.fixture(scope="module")
def repo_registry() -> RoleRegistry:
    return RoleRegistry.from_config(load_config(CONFIG))


# --------------------------------------------------------------------------- #
# canonical keys
# --------------------------------------------------------------------------- #


def test_repository_registry_has_the_sixteen_contract_keys(repo_registry: RoleRegistry) -> None:
    assert set(repo_registry.roles) == set(ROLE_LLM_KEYS)
    assert len(repo_registry.roles) == 16
    assert set(ROLE_ALIASES) == {"technical", "basic", "sentiment", "unlock"}


def test_seven_enabled_analysts_and_five_free_custom_seats(repo_registry: RoleRegistry) -> None:
    assert {spec.key for spec in repo_registry.enabled_analysts()} == set(ANALYSTS)
    assert repo_registry.max_roles == 12
    assert repo_registry.max_custom_roles == 5
    assert set(repo_registry.weights.values()) == {1.0}


# --------------------------------------------------------------------------- #
# aliases
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [("technical", "market"), ("basic", "fundamentals"),
     ("sentiment", "social"), ("unlock", "lockup")],
)
def test_aliases_resolve_to_the_canonical_role(
    repo_registry: RoleRegistry, alias: str, canonical: str
) -> None:
    assert repo_registry.canonical(alias) == canonical
    assert repo_registry.get(alias).key == canonical


def test_alias_does_not_create_an_extra_seat(repo_registry: RoleRegistry) -> None:
    assert len(repo_registry.enabled_analysts()) == 7
    assert "technical" not in repo_registry.roles


def test_alias_colliding_with_a_real_role_is_rejected() -> None:
    roles = {
        "market": role(alias=["technical"]),
        "technical": role(),
    }

    with pytest.raises(RoleError, match="别名"):
        registry_for(roles)


def test_unknown_role_lists_the_available_keys(repo_registry: RoleRegistry) -> None:
    with pytest.raises(RoleError, match="未知角色: ghost"):
        repo_registry.canonical("ghost")


# --------------------------------------------------------------------------- #
# seat caps
# --------------------------------------------------------------------------- #


def test_twelve_analyst_seats_are_accepted() -> None:
    registry = registry_for({**builtin_roles(), **custom_roles(5)})

    assert len(registry.enabled_analysts()) == 12


def test_thirteenth_analyst_seat_is_rejected() -> None:
    with pytest.raises(RoleError, match="超过上限 12"):
        registry_for({**builtin_roles(), **custom_roles(6)})


def test_sixth_custom_seat_is_rejected_even_below_the_global_cap() -> None:
    roles = {key: role() for key in ANALYSTS[:5]}
    roles.update(custom_roles(6))

    with pytest.raises(RoleError, match="自定义分析师，超过上限 5"):
        registry_for(roles)


def test_disabled_analysts_do_not_consume_seats() -> None:
    customs = custom_roles(6)
    customs["custom_5"]["enabled"] = False

    registry = registry_for({**builtin_roles(), **customs})

    assert len(registry.roles) == 13
    assert len(registry.enabled_analysts()) == 12


def test_custom_limit_can_be_lowered_by_config() -> None:
    with pytest.raises(RoleError, match="自定义分析师"):
        registry_for(
            {**builtin_roles(), **custom_roles(2)},
            limits={"max_roles": 12, "max_custom_roles": 1},
        )


# --------------------------------------------------------------------------- #
# overrides
# --------------------------------------------------------------------------- #


def test_weights_are_applied_by_key_and_by_alias(repo_registry: RoleRegistry) -> None:
    adjusted = repo_registry.with_overrides(weights=["market=0.6", "technical=0.4"])

    assert adjusted.weights["market"] == pytest.approx(0.4)
    assert adjusted.weights["news"] == pytest.approx(1.0)
    assert repo_registry.weights["market"] == pytest.approx(1.0)


def test_weights_accept_a_mapping(repo_registry: RoleRegistry) -> None:
    adjusted = repo_registry.with_overrides(weights={"bull": "0.3"})

    assert adjusted.weights["bull"] == pytest.approx(0.3)


@pytest.mark.parametrize(
    ("weight", "message"),
    [
        ("market", "需要形如"),
        ("market=abc", "不是数字"),
        ("market=0", "必须大于 0"),
        ("market=-1", "必须大于 0"),
        ("ghost=1", "未知角色"),
    ],
)
def test_invalid_weight_overrides_are_rejected(
    repo_registry: RoleRegistry, weight: str, message: str
) -> None:
    with pytest.raises(RoleError, match=message):
        repo_registry.with_overrides(weights=[weight])


def test_disable_accepts_an_alias(repo_registry: RoleRegistry) -> None:
    adjusted = repo_registry.with_overrides(disable=["sentiment"])

    assert adjusted.get("social").enabled is False
    assert "social" not in {spec.key for spec in adjusted.enabled_analysts()}


def test_enable_flips_a_disabled_role() -> None:
    roles = builtin_roles()
    roles["news"]["enabled"] = False
    registry = registry_for(roles)

    assert "news" not in {spec.key for spec in registry.enabled_analysts()}
    assert "news" in {
        spec.key for spec in registry.with_overrides(enable=["news"]).enabled_analysts()
    }


def test_temporary_overrides_never_mutate_the_source(repo_registry: RoleRegistry) -> None:
    repo_registry.with_overrides(disable=["news"], weights=["market=0.2"])

    assert repo_registry.get("news").enabled is True
    assert repo_registry.weights["market"] == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# safety switches
# --------------------------------------------------------------------------- #


def test_write_tools_must_stay_denied() -> None:
    with pytest.raises(RoleError, match="不做实盘交易"):
        registry_for(
            builtin_roles(), tools={"enforce": True, "deny_write_tools": False}
        )


def test_role_without_kind_is_rejected() -> None:
    roles = {"market": role(kind="")}

    with pytest.raises(RoleError, match="kind 必填"):
        registry_for(roles)


# --------------------------------------------------------------------------- #
# CLI surface (documented contract: aliases are normalised)
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    strict=False,
    reason="defect QUOT-51-06: `role show technical` looks up the raw TOML table and "
    "exits 3; cli-reference promises aliases are normalised for role commands",
)
def test_cli_role_show_accepts_an_alias() -> None:
    result = run_cli("role", "show", "technical", "--config", str(CONFIG), "--output", "json")

    assert result.returncode == 0
    assert "市场分析师" in result.stdout
