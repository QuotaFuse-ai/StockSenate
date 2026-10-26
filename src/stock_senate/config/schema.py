"""Configuration validation.

Two contracts matter more than the rest and are enforced here:

* ``role_llms`` accepts exactly the sixteen supported role keys.
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
QUALITY_GATE_MODES = ("rules", "judge", "hybrid")
TRACE_EXPORTERS = ("none", "jsonl", "otlp")

_ANY = "*"

_ROLE_ENTRY = {
    "display_name",
    "kind",
    "alias",
    "prompt",
    "tools",
    "output_schema",
    "enabled",
    "weight",
}
_LLM_ROLE_ENTRY = {"provider", "model"}
_STRUCTURED = {"enabled", "max_local_retries", "mark_partial_on_failure"}
_FALLBACK = {"chain": None}

# Closed tables are enumerated; ``_ANY`` marks name-keyed tables whose child
# names are user-defined. A ``None`` value stops the walk (scalars/lists).
_KNOWN_KEYS: dict[str, dict[str, Any]] = {
    "llm_config.toml": {
        "llm": {
            "llm_provider": None,
            "deep_think_llm": None,
            "quick_think_llm": None,
            "backend_url": None,
            "max_tokens": None,
            "output_language": None,
            "market_lookback_days": None,
            "llm_timeout": None,
            "llm_max_retries": None,
            "llm_retry_delay": None,
            "api_key": None,
            "api_key_env": None,
            "role_llms": {_ANY: _LLM_ROLE_ENTRY},
            "allowed_keys": {"keys": None, "strict": None},
            "structured_output": _STRUCTURED,
            "fallback": _FALLBACK,
        },
        "structured_output": _STRUCTURED,
        "fallback": _FALLBACK,
    },
    "agent_roles.toml": {
        "roles": {_ANY: _ROLE_ENTRY},
        "custom": {_ANY: _ROLE_ENTRY},
        "limits": {"max_roles": None, "max_custom_roles": None},
        "tool_permissions": {"enforce": None, "deny_write_tools": None},
    },
    "workflow.toml": {
        "topology": {"pipeline": None, "analyst_inputs": None},
        "debate": {
            "max_debate_rounds": None,
            "max_risk_discuss_rounds": None,
            "convergence_enabled": None,
            "convergence_min_new_points_ratio": None,
            "convergence_max_rounds": None,
        },
        "hitl": {
            "enabled": None,
            "checkpoints": None,
            "on_timeout": None,
            "timeout_seconds": None,
        },
        "checkpoint": {"enabled": None, "backend": None, "path": None, "resume_by": None},
        "logging": {
            "file_enabled": None,
            "file_path": None,
            "max_bytes": None,
            "backup_count": None,
        },
        "incremental": {"enabled": None, "reuse_within_seconds": None, "invalidate_on": None},
        "quality_gate": {
            "enabled": None,
            "threshold": None,
            "block_on_fail": None,
            "dimensions": None,
            "mode": None,
            "judge_weight": None,
        },
        "compression": {
            "enabled": None,
            "trigger_tokens": None,
            "chunk_tokens": None,
            "keep_citations": None,
        },
        "memory": {
            "enabled": None,
            "path": None,
            "max_entries": None,
        },
        "tracing": {
            "enabled": None,
            "exporter": None,
            "path": None,
            "endpoint": None,
            "service_name": None,
            "headers_env": None,
            "timeout_seconds": None,
        },
        "budget": {"max_llm_calls": None, "max_total_tokens": None, "max_wall_clock_seconds": None},
        "queue": {
            "max_attempts": None,
            "retry_delay_seconds": None,
            "on_failure": None,
            "stale_running_seconds": None,
        },
    },
    "data_source.toml": {
        "global": {
            "mode": None,
            "allow_fallback": None,
            "forbid_llm_fill": None,
            "default_timeout_seconds": None,
            "max_retries": None,
        },
        # Providers are pluggable: their keys depend on ``kind``.
        "providers": {_ANY: None},
        "routing": {_ANY: None},
        "freshness": {
            "realtime_max_seconds": None,
            "cached_max_seconds": None,
            "enforce_no_lookahead": None,
        },
    },
    "alert_rules.toml": {
        "global": {
            "enabled": None,
            "dedup_hours": None,
            "quiet_hours": None,
            "max_triggers_per_day": None,
        },
        # Channel tables carry channel-specific keys (URLs, headers, ...).
        "channels": {_ANY: None},
        "rules": {
            "id": None,
            "name": None,
            "level": None,
            "when": None,
            "cooldown_minutes": None,
            "channels": None,
            "enabled": None,
        },
    },
    "budget.toml": {
        "per_run": {
            "max_llm_calls": None,
            "max_input_tokens": None,
            "max_output_tokens": None,
            "max_wall_clock_seconds": None,
            "on_exceed": None,
            "max_cost_cny": None,
        },
        "per_queue": {
            "max_llm_calls": None,
            "max_total_tokens": None,
            "max_wall_clock_seconds": None,
            "max_cost_cny": None,
        },
        "daily": {"max_llm_calls": None, "max_total_tokens": None, "max_cost_cny": None},
        "circuit_breaker": {
            "enabled": None,
            "failure_threshold": None,
            "open_seconds": None,
            "max_open_seconds": None,
            "half_open_probes": None,
        },
        "pricing": {"table": {_ANY: None}},
        "reporting": {"record_usage": None, "store": None, "path": None},
    },
    "output_template.toml": {
        "meta": {
            "title": None,
            "language": None,
            "include_disclaimer": None,
            "include_data_envelope": None,
        },
        "sections": {
            _ANY: {
                "title": None,
                "source": None,
                "per_role": None,
                "forbidden_fields": None,
                "fields": None,
                "show_dimensions": None,
            }
        },
        "export": {
            "formats": None,
            "optional_formats": None,
            "snapshot": None,
            "diff_fields": None,
        },
        "disclaimer": {"text": None},
    },
}


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


def _check_unknown_keys(
    name: str,
    data: dict[str, Any],
    errors: list[dict[str, str]],
    warnings: list[dict[str, str]],
    strict: bool,
) -> None:
    spec = _KNOWN_KEYS.get(name)
    if spec is None:
        return
    _walk_unknown(name, "", data, spec, errors, warnings, strict)


def _walk_unknown(
    file: str,
    prefix: str,
    data: Any,
    spec: Any,
    errors: list[dict[str, str]],
    warnings: list[dict[str, str]],
    strict: bool,
) -> None:
    if isinstance(data, list):
        for index, item in enumerate(data):
            _walk_unknown(file, f"{prefix}[{index}]", item, spec, errors, warnings, strict)
        return
    if not isinstance(data, dict):
        return
    if isinstance(spec, (set, frozenset)):
        spec = {key: None for key in spec}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if key in spec:
            child = spec[key]
        elif _ANY in spec:
            child = spec[_ANY]
        else:
            entry = {
                "file": file,
                "key": path,
                "message": "未知配置键，不会生效；拼写错误请改正，自定义键请确认已支持",
            }
            (errors if strict else warnings).append(entry)
            continue
        if child is not None:
            _walk_unknown(file, path, value, child, errors, warnings, strict)


def validate_dir(directory: Path, *, strict: bool = False) -> dict[str, Any]:
    """Validate every config file in ``directory``.

    Unknown keys always produce a diagnostic. They are warnings by default and
    errors when ``strict`` is set, so a typo cannot pass unnoticed while a
    future-compatible hand-written key still loads.
    """
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

    for name, data in loaded.items():
        _check_unknown_keys(name, data, errors, warnings, strict)
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
                "message": f"缺少内置角色：{sorted(missing_builtin)}（禁用可以，删除需确认）",
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

    checkpoint = data.get("checkpoint", {})
    if checkpoint.get("enabled") is True:
        backend = str(checkpoint.get("backend") or "sqlite").lower()
        if backend != "sqlite":
            _error(
                errors,
                "workflow.toml",
                "checkpoint.backend",
                "当前仅支持 sqlite；其他后端的检查点尚未实现",
            )
        if not str(checkpoint.get("path") or "").strip():
            _error(errors, "workflow.toml", "checkpoint.path", "开启断点续跑时必须配置路径")
        unknown = set(checkpoint.get("resume_by") or []) - {"code", "date", "config_hash"}
        if unknown:
            _error(
                errors,
                "workflow.toml",
                "checkpoint.resume_by",
                f"不支持的分区字段：{sorted(unknown)}",
            )

    logging_cfg = data.get("logging", {})
    if (
        logging_cfg.get("file_enabled") is True
        and not str(logging_cfg.get("file_path") or "").strip()
    ):
        _error(errors, "workflow.toml", "logging.file_path", "开启文件日志时必须配置路径")
    for key in ("max_bytes", "backup_count"):
        value = logging_cfg.get(key)
        if value is not None and (not isinstance(value, int) or value < 0):
            _error(errors, "workflow.toml", f"logging.{key}", "必须是非负整数")
    if logging_cfg.get("max_bytes") == 0:
        _error(errors, "workflow.toml", "logging.max_bytes", "必须大于 0")

    gate = data.get("quality_gate", {})
    threshold = gate.get("threshold", 70)
    if not isinstance(threshold, (int, float)) or not 0 <= threshold <= 100:
        _error(errors, "workflow.toml", "quality_gate.threshold", "必须在 0~100 之间")

    mode = gate.get("mode", "rules")
    if mode not in QUALITY_GATE_MODES:
        _error(errors, "workflow.toml", "quality_gate.mode", "必须是 rules、judge 或 hybrid")
    weight = gate.get("judge_weight", 0.5)
    if not isinstance(weight, (int, float)) or isinstance(weight, bool) or not 0 <= weight <= 1:
        _error(errors, "workflow.toml", "quality_gate.judge_weight", "必须在 0~1 之间")

    compression = data.get("compression", {})
    for key in ("enabled", "keep_citations"):
        value = compression.get(key, True)
        if not isinstance(value, bool):
            _error(errors, "workflow.toml", f"compression.{key}", "必须是布尔值")
    trigger = compression.get("trigger_tokens", 24000)
    chunk = compression.get("chunk_tokens", 6000)
    for key, value in (("trigger_tokens", trigger), ("chunk_tokens", chunk)):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            _error(errors, "workflow.toml", f"compression.{key}", "必须是 > 0 的整数")
    if (
        isinstance(trigger, int)
        and isinstance(chunk, int)
        and not isinstance(trigger, bool)
        and not isinstance(chunk, bool)
        and chunk > 0
        and trigger <= chunk
    ):
        _error(
            errors,
            "workflow.toml",
            "compression.trigger_tokens",
            "必须大于 compression.chunk_tokens，否则单块就超阈值",
        )

    memory = data.get("memory", {})
    if not isinstance(memory.get("enabled", True), bool):
        _error(errors, "workflow.toml", "memory.enabled", "必须是布尔值")
    if not str(memory.get("path") or "memory/reflections.jsonl").strip():
        _error(errors, "workflow.toml", "memory.path", "不能为空")
    entries = memory.get("max_entries", 6)
    if not isinstance(entries, int) or isinstance(entries, bool) or entries < 0:
        _error(errors, "workflow.toml", "memory.max_entries", "必须是 >= 0 的整数")

    tracing_cfg = data.get("tracing", {})
    if not isinstance(tracing_cfg.get("enabled", False), bool):
        _error(errors, "workflow.toml", "tracing.enabled", "必须是布尔值")
    exporter = str(tracing_cfg.get("exporter") or "none").strip().lower()
    if exporter not in TRACE_EXPORTERS:
        _error(errors, "workflow.toml", "tracing.exporter", "可选 none / jsonl / otlp")
    if exporter == "jsonl" and not str(tracing_cfg.get("path") or "").strip():
        _error(errors, "workflow.toml", "tracing.path", "jsonl 导出器必须配置路径")
    if (
        exporter == "otlp"
        and tracing_cfg.get("enabled") is True
        and not str(tracing_cfg.get("endpoint") or "").strip()
    ):
        _error(errors, "workflow.toml", "tracing.endpoint", "otlp 导出器必须配置 endpoint")
    timeout = tracing_cfg.get("timeout_seconds", 5)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        _error(errors, "workflow.toml", "tracing.timeout_seconds", "必须是 > 0 的数值")

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
