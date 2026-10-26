"""Configuration loading: defaults -> user directory -> explicit overrides.

The package ships seven toml files at the repository root. A user directory
(``~/.stock_senate`` by default) may override any subset; files that are absent
are simply inherited. Environment variables beat both, but only for the few
keys documented in ``cli-reference.md``.

Unknown keys are *not* silently accepted by ``schema.validate_dir``; this module
only merges and normalises. It never rewrites a file.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import tomllib
from pathlib import Path
from typing import Any

from stock_senate.observability.logging import get_logger

CONFIG_FILENAMES = (
    "agent_roles.toml",
    "workflow.toml",
    "output_template.toml",
    "data_source.toml",
    "llm_config.toml",
    "alert_rules.toml",
    "budget.toml",
)

_ENV_PREFIX = "STOCKSENATE_"
_LEGACY_PREFIXES = ("TRADINGAGENTS_", "QF_COUNCIL_")
_warned: set[str] = set()

# Minimal fallback so an installed wheel without the repository ``config/``
# directory still produces a runnable (degraded) graph instead of a crash.
_BUILTIN_DEFAULTS: dict[str, Any] = {
    "agent_roles": {
        "roles": {
            "market": {"display_name": "市场分析师", "kind": "analyst", "enabled": True,
                       "tools": ["kline", "quote", "money_flow"], "output_schema": "AnalystReport"},
            "social": {"display_name": "情绪分析师", "kind": "analyst", "enabled": True,
                       "tools": ["social_buzz", "news"], "output_schema": "AnalystReport"},
            "news": {"display_name": "新闻分析师", "kind": "analyst", "enabled": True,
                     "tools": ["news", "announcement"], "output_schema": "AnalystReport"},
            "fundamentals": {"display_name": "基本面分析师", "kind": "analyst", "enabled": True,
                             "tools": ["financial"], "output_schema": "AnalystReport"},
            "policy": {"display_name": "政策分析师", "kind": "analyst", "enabled": True,
                       "tools": ["policy_feed", "news"], "output_schema": "AnalystReport"},
            "hot_money": {"display_name": "游资追踪", "kind": "analyst", "enabled": True,
                          "tools": ["dragon_tiger", "money_flow"], "output_schema": "AnalystReport"},
            "lockup": {"display_name": "解禁观察", "kind": "analyst", "enabled": True,
                       "tools": ["lockup_calendar", "announcement"], "output_schema": "AnalystReport"},
            "bull": {"display_name": "多头研究员", "kind": "researcher", "enabled": True,
                     "tools": [], "output_schema": "ResearchArgument"},
            "bear": {"display_name": "空头研究员", "kind": "researcher", "enabled": True,
                     "tools": [], "output_schema": "ResearchArgument"},
            "research_manager": {"display_name": "研究经理", "kind": "manager", "enabled": True,
                                 "tools": [], "output_schema": "ResearchVerdict"},
            "trader": {"display_name": "交易员", "kind": "trader", "enabled": True,
                       "tools": ["kline", "quote"], "output_schema": "TradePlan"},
            "risk_aggressive": {"display_name": "激进风险辩手", "kind": "risk", "enabled": True,
                                "tools": [], "output_schema": "RiskArgument"},
            "risk_neutral": {"display_name": "中立风险辩手", "kind": "risk", "enabled": True,
                             "tools": [], "output_schema": "RiskArgument"},
            "risk_conservative": {"display_name": "保守风险辩手", "kind": "risk", "enabled": True,
                                  "tools": [], "output_schema": "RiskArgument"},
            "portfolio_manager": {"display_name": "投资经理", "kind": "manager", "enabled": True,
                                  "tools": [], "output_schema": "FinalDecision"},
            "quality_gate": {"display_name": "质量闸门", "kind": "judge", "enabled": True,
                             "tools": [], "output_schema": "QualityVerdict"},
        },
        "custom": {},
        "limits": {"max_roles": 12, "max_custom_roles": 5},
        "tool_permissions": {"enforce": True, "deny_write_tools": True},
    },
    "workflow": {
        "topology": {"pipeline": ["analysts", "bull_bear_debate", "research_manager",
                                  "trader", "risk_debate", "portfolio_manager"]},
        "debate": {"max_debate_rounds": 2, "max_risk_discuss_rounds": 2,
                   "convergence_enabled": True, "convergence_min_new_points_ratio": 0.2},
        "hitl": {"enabled": True, "checkpoints": ["after_trader", "before_final"],
                 "on_timeout": "auto_approve"},
        "quality_gate": {"enabled": True, "threshold": 70, "block_on_fail": False},
        "budget": {"max_llm_calls": 60, "max_total_tokens": 400000, "max_wall_clock_seconds": 1800},
    },
    "output_template": {
        "meta": {"include_disclaimer": True, "include_data_envelope": True},
        "sections": {
            "analysts": {"title": "一、分析师观点"},
            "research": {"title": "二、多空辩论与研究经理结论"},
            "trade": {"title": "三、交易方案",
                      "forbidden_fields": ["order_id", "broker_account", "auto_order"]},
            "risk": {"title": "四、风险辩论"},
            "decision": {"title": "五、投资经理终审", "fields": ["rating", "horizon", "reasons", "risks", "confidence"]},
            "quality": {"title": "六、质量评估"},
            "data": {"title": "附：数据来源与新鲜度"},
        },
        "disclaimer": {"text": "本报告由 AI 自动生成，仅用于研究与教学，不构成任何投资建议，也不包含可执行的交易指令。"},
    },
    "data_source": {
        "global": {"mode": "priority", "allow_fallback": True, "forbid_llm_fill": True},
        "providers": {"local_json": {"kind": "json_file", "root": "data/local",
                                     "layout": "{domain}/{code}/{date}.json"}},
        "routing": {},
        "freshness": {"enforce_no_lookahead": True},
    },
    "llm_config": {
        "llm": {"llm_provider": "offline", "deep_think_llm": "offline",
                "quick_think_llm": "offline", "max_tokens": 4096,
                "market_lookback_days": 60, "llm_timeout": 150, "llm_max_retries": 3},
    },
    "alert_rules": {"global": {"enabled": False}, "channels": {}, "rules": []},
    "budget": {
        "per_run": {"max_llm_calls": 60, "max_input_tokens": 300000,
                    "max_output_tokens": 100000, "max_wall_clock_seconds": 1800,
                    "on_exceed": "stop"},
        "circuit_breaker": {"enabled": True, "failure_threshold": 5,
                            "open_seconds": 60, "max_open_seconds": 900},
    },
}


def default_config_dir() -> Path | None:
    """Locate the repository ``config/`` directory that ships the defaults."""
    override = os.environ.get("STOCKSENATE_DEFAULTS_DIR")
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_dir():
            return candidate
    package_root = Path(__file__).resolve().parents[3]
    candidate = package_root / "config"
    if candidate.is_dir():
        return candidate
    return None


def home_dir() -> Path:
    override = os.environ.get("STOCKSENATE_HOME")
    if override:
        return Path(override).expanduser()
    legacy = _legacy_env("HOME")
    if legacy:
        return Path(legacy).expanduser()
    return Path.home() / ".stock_senate"


def config_dir(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    override = os.environ.get("STOCKSENATE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    legacy = os.environ.get("TRADINGAGENTS_CONFIG_DIR") or os.environ.get("QF_COUNCIL_CONFIG_DIR")
    if legacy:
        _warn_legacy("CONFIG_DIR", legacy, "STOCKSENATE_CONFIG_DIR")
        return Path(legacy).expanduser()
    return Path.home() / ".stock_senate"


def _warn_legacy(key: str, value: str, new_name: str) -> None:
    marker = f"{key}:{value}"
    if marker in _warned:
        return
    _warned.add(marker)
    get_logger("config").warning(
        "legacy environment variable in use; migrate to the STOCKSENATE prefix",
        extra={"event": "legacy_env", "key": key, "value": value, "new_name": new_name},
    )


def _legacy_env(suffix: str) -> str | None:
    for prefix in _LEGACY_PREFIXES:
        value = os.environ.get(prefix + suffix)
        if value:
            _warn_legacy(prefix + suffix, value, _ENV_PREFIX + suffix)
            return value
    return None


def env(name: str, default: str | None = None) -> str | None:
    """Read ``STOCKSENATE_<name>`` with legacy fallback and a migration warning."""
    value = os.environ.get(_ENV_PREFIX + name)
    if value is not None and value != "":
        return value
    legacy = _legacy_env(name)
    if legacy is not None:
        return legacy
    return default


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base`` without mutating either."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def read_config_dir(directory: Path) -> dict[str, Any]:
    """Read every present config file from a directory, without merging."""
    loaded: dict[str, Any] = {}
    directory = Path(directory).expanduser()
    for name in CONFIG_FILENAMES:
        path = directory / name
        if not path.exists():
            continue
        with path.open("rb") as handle:
            loaded[name.removesuffix(".toml")] = tomllib.load(handle)
    return loaded


def _default_config() -> dict[str, Any]:
    directory = default_config_dir()
    if directory is not None:
        try:
            loaded = read_config_dir(directory)
            if loaded:
                return deep_merge(_BUILTIN_DEFAULTS, loaded)
        except (OSError, tomllib.TOMLDecodeError) as exc:  # pragma: no cover - broken checkout
            get_logger("config").warning(
                "failed to read packaged defaults; using built-in minimum",
                extra={"event": "defaults_failed", "error": str(exc)},
            )
    return copy.deepcopy(_BUILTIN_DEFAULTS)


def _is_loaded_config(config: dict[str, Any]) -> bool:
    """True for the dict produced by ``cli._load_config_dir``."""
    return "config_dir" in config and any(
        key in config for key in ("agent_roles", "workflow", "llm_config", "data_source")
    )


def load_config(config: dict[str, Any] | str | Path | None = None) -> dict[str, Any]:
    """Return the effective configuration as a plain dictionary.

    ``config`` may be a directory path, a partial override mapping, the merged
    mapping built by the CLI, or ``None`` for defaults + the user directory.
    """
    cfg = _default_config()

    explicit_dir: Path | None = None
    if isinstance(config, (str, Path)):
        explicit_dir = Path(config).expanduser()
    elif isinstance(config, dict) and _is_loaded_config(config):
        explicit_dir = Path(config.get("config_dir", config_dir())).expanduser()

    user: dict[str, Any] = {}
    if explicit_dir is not None:
        user = read_config_dir(explicit_dir)
    elif config is None or not _is_loaded_config(config or {}):
        directory = config_dir()
        if directory.is_dir():
            user = read_config_dir(directory)

    cfg = deep_merge(cfg, user)

    if isinstance(config, dict):
        overrides = {
            key: value
            for key, value in config.items()
            if key not in {"config_dir"} and not _is_loaded_config(config)
        }
        if overrides:
            cfg = deep_merge(cfg, overrides)
        elif _is_loaded_config(config):
            # The CLI already merged files; honour them verbatim.
            for key in CONFIG_FILENAMES:
                stem = key.removesuffix(".toml")
                if stem in config:
                    cfg = deep_merge(cfg, {stem: config[stem]})

    _apply_env(cfg)
    cfg["config_dir"] = str(explicit_dir or config_dir())
    cfg["home"] = str(home_dir())
    cfg["config_hash"] = config_hash(cfg)
    return cfg


def _apply_env(cfg: dict[str, Any]) -> None:
    llm = cfg.setdefault("llm_config", {}).setdefault("llm", {})
    provider = env("LLM_PROVIDER")
    if provider:
        # An explicit provider override is authoritative for every role, so
        # ``STOCKSENATE_LLM_PROVIDER=offline`` really runs the offline engine.
        llm["llm_provider"] = provider
        if provider.lower() == "offline":
            llm["deep_think_llm"] = "offline"
            llm["quick_think_llm"] = "offline"
            for entry in (llm.get("role_llms") or {}).values():
                if isinstance(entry, dict):
                    entry["provider"] = "offline"
                    entry["model"] = "offline"
    base_url = env("LLM_BASE_URL")
    if base_url:
        llm["backend_url"] = base_url
    mode = env("DATA_MODE")
    if mode:
        cfg.setdefault("data_source", {}).setdefault("global", {})["mode"] = mode


def config_hash(cfg: dict[str, Any]) -> str:
    """Stable hash of the semantic configuration (paths excluded)."""
    clean = {key: value for key, value in cfg.items() if key not in {"config_dir", "home", "config_hash"}}
    blob = json.dumps(clean, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def ensure_home(home: Path | None = None) -> Path:
    """Create the state directory tree and return its root."""
    root = Path(home).expanduser() if home else home_dir()
    for child in ("reports", "cache", "logs", "locks"):
        (root / child).mkdir(parents=True, exist_ok=True)
    return root


def migrate_legacy(target: Path | None = None, *, dry_run: bool = False) -> dict[str, Any]:
    """Copy ``~/.tradingagents`` into the new home; never touch the source."""
    legacy = Path.home() / ".tradingagents"
    destination = Path(target).expanduser() if target else home_dir()
    if not legacy.exists():
        return {"migrated": False, "reason": "legacy directory absent",
                "source": str(legacy), "target": str(destination)}
    if dry_run:
        return {"migrated": False, "dry_run": True,
                "source": str(legacy), "target": str(destination)}
    destination.mkdir(parents=True, exist_ok=True)
    migrated: list[str] = []
    for item in legacy.iterdir():
        target_path = destination / item.name
        if target_path.exists():
            continue
        if item.is_dir():
            shutil.copytree(item, target_path)
        else:
            shutil.copy2(item, target_path)
        migrated.append(item.name)
    return {"migrated": True, "source": str(legacy), "target": str(destination),
            "copied": migrated, "source_untouched": True}


__all__ = [
    "CONFIG_FILENAMES",
    "config_dir",
    "config_hash",
    "deep_merge",
    "default_config_dir",
    "ensure_home",
    "env",
    "home_dir",
    "load_config",
    "migrate_legacy",
    "read_config_dir",
]
