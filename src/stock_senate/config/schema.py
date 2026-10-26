"""Configuration validation.

Two contracts matter more than the rest and are enforced here:

* ``role_llms`` accepts exactly the sixteen keys the upstream project supports.
  A typo must fail loudly instead of silently falling back to the default
  model, because a silent fallback looks like a working run with the wrong
  model.
* At most twelve analyst seats (seven built-in plus five custom) may be
  enabled. Researchers, the trader, the risk debaters and the managers are
  fixed workflow stages and do not consume a seat (DEC-005).

The validator never rewrites files; ``ok`` plus a list of ``{file, key,
message}`` entries is what the CLI prints.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

REQUIRED_FILES = (
    "agent_roles.toml",
    "workflow.toml",
    "output_template.toml",
    "data_source.toml",
    "llm_config.toml",
    "alert_rules.toml",
    "budget.toml",
)

ROLE_LLM_KEYS = (
    "market",
    "social",
    "news",
    "fundamentals",
    "policy",
    "hot_money",
    "lockup",
    "bull",
    "bear",
    "research_manager",
    "trader",
    "risk_aggressive",
    "risk_neutral",
    "risk_conservative",
    "quality_gate",
    "portfolio_manager",
)

ROLE_ALIASES = {
    "technical": "market",
    "basic": "fundamentals",
    "sentiment": "social",
    "unlock": "lockup",
}

BUILTIN_ANALYSTS = {"market", "social", "news", "fundamentals", "policy", "hot_money", "lockup"}
BUILTIN_ROLES = set(ROLE_LLM_KEYS) - {"quality_gate"}

DATA_MODES = {"stockdata", "local", "hybrid", "priority"}
ALERT_LEVELS = {"high", "mid", "low"}
ON_EXCEED = {"stop", "fail"}
HITL_CHECKPOINTS = {"after_trader", "before_final"}


def _error(errors: list[dict[str, str]], file: str, key: str, message: str) -> None:
    errors.append({"file": file, "key": key, "message": message})


def _load(directory: Path, name: str, errors: list[dict[str, str]]) -> dict[str, Any] | None:
    path = directory / name
    if not path.exists():
        _error(errors, name, "", "文件缺失：`ssenate config init` 生成模板后再校验")
        return None
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        _error(errors, name, "", f"TOML 解析失败：{exc}")
        return None


def validate_dir(directory: Path) -> dict[str, Any]:
    """Validate every config file in ``directory``."""
    directory = Path(directory).expanduser()
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    if not directory.is_dir():
        _error(errors, str(directory), "", "配置目录不存在：`ssenate config init` 可创建")
        return {"ok": False, "config_dir": str(directory), "errors": errors, "warnings": warnings}

    loaded: dict[str, dict[str, Any]] = {}
    for name in REQUIRED_FILES:
        data = _load(directory, name, errors)
        if data is not None:
            loaded[name] = data

    if "llm_config.toml" in loaded:
        _check_llm(loaded["llm_config.toml"], errors, warnings)
    if "agent_roles.toml" in loaded:
        _check_roles_file(loaded["agent_roles.toml"], errors, warnings)
    if "workflow.toml" in loaded:
        _check_workflow(loaded["workflow.toml"], errors)
    if "data_source.toml" in loaded:
        _check_data_source(loaded["data_source.toml"], errors, warnings)
    if "alert_rules.toml" in loaded:
        _check_alerts(loaded["alert_rules.toml"], errors)
    if "budget.toml" in loaded:
        _check_budget(loaded["budget.toml"], errors)
    if "output_template.toml" in loaded:
        _check_output(loaded["output_template.toml"], errors)

    return {
        "ok": not errors,
        "config_dir": str(directory),
        "errors": errors,
        "warnings": warnings,
    }


def validate_roles(roles: dict[str, Any]) -> dict[str, Any]:
    """Validate a parsed ``[roles.*]`` table.

    The write-tool gate lives on the file level and is checked by
    ``validate_dir``; this entry point only inspects the role table.
    """
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    _check_roles({"roles": roles, "tool_permissions": {"deny_write_tools": True}}, errors, warnings)
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def validate_alert_rules(config: dict[str, Any]) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    _check_alerts(config.get("alert_rules", {}), errors)
    return {"ok": not errors, "errors": errors, "warnings": []}


# --------------------------------------------------------------------------- #
# per-file rules
# --------------------------------------------------------------------------- #


def _check_llm(
    data: dict[str, Any], errors: list[dict[str, str]], warnings: list[dict[str, str]]
) -> None:
    llm = data.get("llm", {})
    for key in ("llm_provider", "deep_think_llm", "quick_think_llm"):
        if not llm.get(key):
            _error(errors, "llm_config.toml", f"llm.{key}", "必填且不能为空")

    for key in ("max_tokens", "market_lookback_days", "llm_timeout", "llm_max_retries"):
        value = llm.get(key)
        if value is not None and (not isinstance(value, int) or value <= 0):
            _error(errors, "llm_config.toml", f"llm.{key}", "必须是正整数")

    role_llms = llm.get("role_llms", {})
    seen: dict[str, str] = {}
    for key, value in role_llms.items():
        canonical = ROLE_ALIASES.get(key, key)
        if canonical not in ROLE_LLM_KEYS:
            _error(
                errors,
                "llm_config.toml",
                f"llm.role_llms.{key}",
                f"非法角色键；合法键为 {', '.join(ROLE_LLM_KEYS)}",
            )
            continue
        if canonical in seen:
            _error(
                errors,
                "llm_config.toml",
                f"llm.role_llms.{key}",
                f"与 `{seen[canonical]}` 指向同一角色（别名冲突）",
            )
        seen[canonical] = key
        if not isinstance(value, dict) or not value.get("provider") or not value.get("model"):
            _error(errors, "llm_config.toml", f"llm.role_llms.{key}", "需要 provider 与 model")
        if key != canonical:
            warnings.append(
                {
                    "file": "llm_config.toml",
                    "key": f"llm.role_llms.{key}",
                    "message": f"别名请改为规范键 `{canonical}`",
                }
            )

    allowed = llm.get("allowed_keys", {})
    if allowed.get("strict", True):
        declared = set(allowed.get("keys", ROLE_LLM_KEYS))
        unknown = declared - set(ROLE_LLM_KEYS)
        if unknown:
            _error(
                errors, "llm_config.toml", "llm.allowed_keys.keys", f"包含未知键：{sorted(unknown)}"
            )


def _check_roles_file(
    data: dict[str, Any], errors: list[dict[str, str]], warnings: list[dict[str, str]]
) -> None:
    _check_roles(data, errors, warnings)
    limits = data.get("limits", {})
    max_custom = limits.get("max_custom_roles", 5)
    roles = data.get("roles", {})
    custom_analysts = [
        key
        for key, value in roles.items()
        if (value or {}).get("kind") == "analyst"
        and (value or {}).get("enabled", True)
        and key not in BUILTIN_ANALYSTS
    ]
    if len(custom_analysts) > max_custom:
        _error(
            errors,
            "agent_roles.toml",
            "limits.max_custom_roles",
            f"启用 {len(custom_analysts)} 个自定义分析师，超过上限 {max_custom}",
        )


def _check_roles(
    data: dict[str, Any], errors: list[dict[str, str]], warnings: list[dict[str, str]]
) -> None:
    roles = data.get("roles", {})
    limits = data.get("limits", {})
    max_roles = limits.get("max_roles", 12)

    if not roles:
        _error(errors, "agent_roles.toml", "roles", "至少需要一个角色")

    enabled = [key for key, value in roles.items() if (value or {}).get("enabled", True)]
    analysts = [key for key in enabled if (roles.get(key) or {}).get("kind") == "analyst"]
    # 角色上限约束的是分析师席位（7 内置 + 5 自定义 = 12）。
    # 多空研究员、交易员、风险辩手、经理是固定工作流阶段，不计入该上限，
    # 否则与「7 内置」的定义自相矛盾（见 DEC-005）。
    if len(analysts) > max_roles:
        _error(
            errors,
            "agent_roles.toml",
            "limits.max_roles",
            f"启用 {len(analysts)} 个分析师席位，超过上限 {max_roles}（7 内置 + 5 自定义）",
        )

    for key, value in roles.items():
        value = value or {}
        if not value.get("kind"):
            _error(
                errors,
                "agent_roles.toml",
                f"roles.{key}.kind",
                "必填：analyst/researcher/manager/trader/risk/judge",
            )
        if not value.get("output_schema"):
            _error(
                errors,
                "agent_roles.toml",
                f"roles.{key}.output_schema",
                "必填：Pydantic Schema 名称",
            )
        if value.get("kind") == "analyst" and not value.get("tools"):
            warnings.append(
                {
                    "file": "agent_roles.toml",
                    "key": f"roles.{key}.tools",
                    "message": "分析师没有工具白名单，将只能使用上下文",
                }
            )

    missing_builtin = BUILTIN_ROLES - set(roles)
    if missing_builtin:
        warnings.append(
            {
                "file": "agent_roles.toml",
                "key": "roles",
                "message": f"缺少上游内置角色：{sorted(missing_builtin)}（禁用可以，删除需确认）",
            }
        )

    if (data.get("tool_permissions") or {}).get("deny_write_tools") is not True:
        _error(
            errors,
            "agent_roles.toml",
            "tool_permissions.deny_write_tools",
            "必须为 true：本系统不做实盘交易",
        )


def _check_workflow(data: dict[str, Any], errors: list[dict[str, str]]) -> None:
    topology = data.get("topology", {})
    pipeline = topology.get("pipeline", [])
    for stage in ("analysts", "research_manager", "trader", "portfolio_manager"):
        if stage not in pipeline:
            _error(
                errors,
                "workflow.toml",
                "topology.pipeline",
                f"缺少必需阶段 `{stage}`；终审不可跳过",
            )
    if pipeline and pipeline[-1] != "portfolio_manager":
        _error(errors, "workflow.toml", "topology.pipeline", "最后一步必须是 portfolio_manager")

    debate = data.get("debate", {})
    for key in ("max_debate_rounds", "max_risk_discuss_rounds"):
        value = debate.get(key, 1)
        if not isinstance(value, int) or value < 1:
            _error(errors, "workflow.toml", f"debate.{key}", "必须是 >= 1 的整数")

    hitl = data.get("hitl", {})
    for checkpoint in hitl.get("checkpoints", []):
        if checkpoint not in HITL_CHECKPOINTS:
            _error(errors, "workflow.toml", "hitl.checkpoints", f"未知检查点 `{checkpoint}`")
    if hitl.get("on_timeout") not in {"auto_approve", "abort", None}:
        _error(errors, "workflow.toml", "hitl.on_timeout", "可选 auto_approve / abort")

    gate = data.get("quality_gate", {})
    threshold = gate.get("threshold", 70)
    if not isinstance(threshold, (int, float)) or not 0 <= threshold <= 100:
        _error(errors, "workflow.toml", "quality_gate.threshold", "必须在 0~100 之间")

    queue = data.get("queue", {})
    attempts = queue.get("max_attempts", 3)
    if not isinstance(attempts, int) or attempts < 1:
        _error(errors, "workflow.toml", "queue.max_attempts", "必须是 >= 1 的整数")
    delay = queue.get("retry_delay_seconds", 5)
    if not isinstance(delay, (int, float)) or delay < 0:
        _error(errors, "workflow.toml", "queue.retry_delay_seconds", "必须是 >= 0 的数值")
    if queue.get("on_failure", "skip") not in {"skip", "stop"}:
        _error(errors, "workflow.toml", "queue.on_failure", "可选 skip / stop")
    stale = queue.get("stale_running_seconds", 21600)
    if not isinstance(stale, (int, float)) or stale <= 0:
        _error(errors, "workflow.toml", "queue.stale_running_seconds", "必须是 > 0 的数值")


def _check_data_source(
    data: dict[str, Any], errors: list[dict[str, str]], warnings: list[dict[str, str]]
) -> None:
    global_cfg = data.get("global", {})
    mode = global_cfg.get("mode", "priority")
    if mode not in DATA_MODES:
        _error(errors, "data_source.toml", "global.mode", f"可选 {sorted(DATA_MODES)}")
    if global_cfg.get("forbid_llm_fill") is not True:
        _error(
            errors,
            "data_source.toml",
            "global.forbid_llm_fill",
            "必须为 true：数据缺失不得由模型补数值",
        )

    providers = data.get("providers", {})
    routing = data.get("routing", {})
    for domain, order in routing.items():
        for name in order:
            if name not in providers:
                _error(
                    errors,
                    "data_source.toml",
                    f"routing.{domain}",
                    f"引用了未定义的 provider `{name}`",
                )
    for name, cfg in providers.items():
        if (cfg or {}).get("kind") == "text_doc" and (cfg or {}).get(
            "numeric_extraction"
        ) is not False:
            _error(
                errors,
                "data_source.toml",
                f"providers.{name}.numeric_extraction",
                "文本资料只做定性阅读，必须为 false",
            )

    freshness = data.get("freshness", {})
    if freshness.get("enforce_no_lookahead") is not True:
        _error(
            errors,
            "data_source.toml",
            "freshness.enforce_no_lookahead",
            "必须为 true：回测禁止未来函数",
        )


def _check_alerts(data: dict[str, Any], errors: list[dict[str, str]]) -> None:
    rules = data.get("rules", [])
    if not rules:
        errors.append({"file": "alert_rules.toml", "key": "rules", "message": "至少需要一条规则"})
        return
    seen: set[str] = set()
    for index, rule in enumerate(rules):
        prefix = f"rules[{index}]"
        rule_id = rule.get("id")
        if not rule_id:
            _error(errors, "alert_rules.toml", f"{prefix}.id", "必填")
        elif rule_id in seen:
            _error(errors, "alert_rules.toml", f"{prefix}.id", f"重复的规则 id `{rule_id}`")
        else:
            seen.add(rule_id)
        if rule.get("level") not in ALERT_LEVELS:
            _error(errors, "alert_rules.toml", f"{prefix}.level", "可选 high / mid / low")
        if not rule.get("when"):
            _error(errors, "alert_rules.toml", f"{prefix}.when", "条件表达式不能为空")
        cooldown = rule.get("cooldown_minutes")
        if cooldown is not None and (not isinstance(cooldown, int) or cooldown < 0):
            _error(errors, "alert_rules.toml", f"{prefix}.cooldown_minutes", "必须是非负整数")

    channels = data.get("channels", {})
    for index, rule in enumerate(rules):
        for channel in rule.get("channels", []):
            if channel not in channels:
                _error(
                    errors,
                    "alert_rules.toml",
                    f"rules[{index}].channels",
                    f"未定义的通道 `{channel}`",
                )

    webhook = channels.get("webhook", {})
    if webhook.get("enabled"):
        for key in ("url_env", "secret_env"):
            if not webhook.get(key):
                _error(
                    errors, "alert_rules.toml", f"channels.webhook.{key}", "启用 webhook 时必须配置"
                )


def _check_budget(data: dict[str, Any], errors: list[dict[str, str]]) -> None:
    per_run = data.get("per_run", {})
    if per_run.get("on_exceed") not in ON_EXCEED:
        _error(errors, "budget.toml", "per_run.on_exceed", "可选 stop / fail")
    for key, value in per_run.items():
        if key in {"on_exceed", "max_cost_cny"}:
            continue
        if key.startswith("max_") and (not isinstance(value, int) or value <= 0):
            _error(errors, "budget.toml", f"per_run.{key}", "必须是正整数")

    breaker = data.get("circuit_breaker", {})
    if breaker.get("enabled", True):
        if breaker.get("failure_threshold", 5) < 1:
            _error(errors, "budget.toml", "circuit_breaker.failure_threshold", "必须 >= 1")
        if breaker.get("open_seconds", 60) > breaker.get("max_open_seconds", 900):
            _error(
                errors, "budget.toml", "circuit_breaker.open_seconds", "不能大于 max_open_seconds"
            )


def _check_output(data: dict[str, Any], errors: list[dict[str, str]]) -> None:
    sections = data.get("sections", {})
    for key in ("analysts", "research", "trade", "risk", "decision"):
        if key not in sections:
            _error(errors, "output_template.toml", f"sections.{key}", "缺少必需章节")
    forbidden = (sections.get("trade") or {}).get("forbidden_fields", [])
    for field in ("order_id", "broker_account", "auto_order"):
        if field not in forbidden:
            _error(
                errors,
                "output_template.toml",
                "sections.trade.forbidden_fields",
                f"必须禁止 `{field}`：本系统不输出交易指令",
            )
    if (data.get("meta") or {}).get("include_disclaimer") is not True:
        _error(errors, "output_template.toml", "meta.include_disclaimer", "必须为 true")
